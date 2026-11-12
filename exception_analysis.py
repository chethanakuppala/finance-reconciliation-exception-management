#!/usr/bin/env python3
"""
exception_analysis.py - AI-assisted exception triage with validation controls.

For every exception in output/exceptions.csv this builds an *evidence pack* -
the exception plus the invoice, payment, journal, bank and vendor context behind
it - and produces a structured investigation summary against a fixed schema:

    root cause hypothesis, confidence, evidence cited, investigation steps,
    recommended action (from a closed list), expected recovery, repeat risk,
    control gap, human-review flag

Two providers, one schema:

  --provider rules   (default) a deterministic analyser that writes the same
                     structured output from the control's own logic. No API key,
                     no network, reproducible - this is the baseline every AI
                     answer is checked against.
  --provider claude  sends each evidence pack to the Claude API with the schema
                     enforced as a structured output, then runs the same
                     validation gate over the response.

Validation gate (V-01 .. V-10) - nothing reaches the register unchecked:

  V-01 schema and type conformance
  V-02 closed-list enums (recommended_action, repeat_risk)
  V-03 confidence bounded 0-1, low confidence routed to a human
  V-04 numeric grounding - every figure in the narrative must trace to the
       evidence pack (no invented amounts)
  V-05 identifier grounding - every document ID mentioned must exist in the pack
  V-06 materiality gate - high value or high severity is always human-reviewed
  V-07 action policy matrix - the recommended action must be permitted for that
       exception type
  V-08 sensitive-data screen - no bank or tax identifiers in the narrative
  V-09 cross-check against the deterministic baseline (recovery estimate and
       action agreement)
  V-10 reproducibility - SHA-256 of the evidence pack is logged and cached, so a
       rerun returns the same answer and does not re-bill the API

A failed blocking check does not produce a "fixed" answer: the record falls back
to the deterministic baseline and is flagged for manual triage. The AI layer
annotates the exception register - it never closes an item, and it never changes
a balance.

Outputs
  output/exception_analysis.csv   one analysed row per exception
  output/exception_analysis.json  records + summary metrics for the dashboard
  output/ai_validation_log.csv    the audit trail: hash in, checks, disposition
  output/ai_cache.json            evidence-pack hash -> provider response

Usage
  python3 exception_analysis.py
  python3 exception_analysis.py --provider claude --limit 25
"""

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
from datetime import datetime

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
CACHE_PATH = os.path.join(OUT, "ai_cache.json")

MODEL = "claude-opus-5-5"

# ------------------------------------------------------------------ policy

RECOMMENDED_ACTIONS = [
    "Recover from vendor",
    "Void and reissue payment",
    "Reverse duplicate accrual",
    "Post correcting journal",
    "Request credit memo",
    "Apply cash to open invoice",
    "Obtain approval evidence",
    "Merge vendor master records",
    "Confirm with bank",
    "Escalate to controller",
    "Write off below threshold",
    "No action - transaction supported",
]

REPEAT_RISK = ["Low", "Medium", "High"]

# V-07: which actions are defensible for which exception type
ACTION_POLICY = {
    "DUPLICATE_INVOICE": ["Reverse duplicate accrual", "Request credit memo",
                          "Recover from vendor", "Escalate to controller",
                          "No action - transaction supported"],
    "DUPLICATE_PAYMENT": ["Recover from vendor", "Request credit memo",
                          "Void and reissue payment", "Escalate to controller"],
    "UNAPPLIED_PAYMENT": ["Apply cash to open invoice", "Recover from vendor",
                          "Escalate to controller", "Write off below threshold"],
    "MISSING_GL_POSTING": ["Post correcting journal", "Escalate to controller"],
    "PAYMENT_AMOUNT_MISMATCH": ["Request credit memo", "Recover from vendor",
                                "Post correcting journal", "Write off below threshold",
                                "No action - transaction supported"],
    "GL_CASH_NOT_ON_STATEMENT": ["Confirm with bank", "Void and reissue payment",
                                 "Post correcting journal", "Escalate to controller"],
    "BANK_ITEM_NOT_IN_GL": ["Post correcting journal", "Confirm with bank",
                            "Write off below threshold"],
    "UNBALANCED_JOURNAL": ["Post correcting journal", "Escalate to controller"],
    "SOD_VIOLATION": ["Obtain approval evidence", "Escalate to controller"],
    "FX_RATE_ERROR": ["Post correcting journal", "Escalate to controller",
                      "Write off below threshold"],
    "TOPSIDE_AP_ENTRY": ["Obtain approval evidence", "Post correcting journal",
                         "Escalate to controller"],
    "GL_SUBLEDGER_VARIANCE": ["Post correcting journal", "Escalate to controller"],
    "DUPLICATE_VENDOR_MASTER": ["Merge vendor master records", "Escalate to controller"],
    "AGED_OPEN_ITEM": ["Request credit memo", "Escalate to controller",
                       "Write off below threshold", "No action - transaction supported"],
}

MATERIALITY_REVIEW_USD = 25_000.00     # V-06
CONFIDENCE_FLOOR = 0.55                # V-03
RECOVERY_DIVERGENCE_TOL = 0.50         # V-09
WRITE_OFF_CEILING_USD = 250.00
MANUAL_TRIAGE_MINUTES = 25             # baseline effort per exception, pre-automation

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "exception_id": {"type": "string"},
        "root_cause_hypothesis": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence_cited": {"type": "array", "items": {"type": "string"}},
        "investigation_steps": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string", "enum": RECOMMENDED_ACTIONS},
        "recovery_expected_usd": {"type": "number"},
        "repeat_risk": {"type": "string", "enum": REPEAT_RISK},
        "control_gap": {"type": "string"},
        "requires_human_review": {"type": "boolean"},
    },
    "required": ["exception_id", "root_cause_hypothesis", "confidence",
                 "evidence_cited", "investigation_steps", "recommended_action",
                 "recovery_expected_usd", "repeat_risk", "control_gap",
                 "requires_human_review"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are an exception analyst on a corporate finance operations team. You triage \
reconciliation exceptions raised by automated controls over the accounts payable \
sub-ledger, the general ledger, the disbursement system and bank statements.

Rules you must follow:
1. Use only the facts in the evidence pack. Never introduce an amount, date, \
document number, vendor or user that is not present in it.
2. Every figure you state must appear in the evidence pack exactly as given.
3. You propose an action; you never conclude that an item is cleared or closed. \
A human owns the disposition.
4. Pick recommended_action only from the list permitted for this exception type.
5. Set confidence to reflect how completely the evidence supports your root \
cause. If the pack is thin, say so and lower the confidence.
6. Never restate bank account numbers, tax identification numbers, or any other \
sensitive identifier in your narrative.
7. Keep root_cause_hypothesis under 60 words and give 2 to 5 concrete \
investigation steps a finance analyst can execute today.
"""


# ---------------------------------------------------------------- evidence

def load_context():
    ctx = {}
    for name in ["invoices", "payments", "gl_entries", "bank_transactions", "vendors"]:
        ctx[name] = pd.read_csv(os.path.join(DATA, f"{name}.csv"),
                                dtype={"gl_account": str, "invoice_number": str,
                                       "bank_account_last4": str})
    ctx["exceptions"] = pd.read_csv(os.path.join(OUT, "exceptions.csv"))
    ctx["tieout"] = pd.read_csv(os.path.join(OUT, "gl_vs_subledger.csv"))
    return ctx


def pick(df, col, val, cols):
    if not val or pd.isna(val):
        return []
    hit = df[df[col].astype(str) == str(val)]
    return hit[cols].to_dict("records")


def build_evidence_pack(exc, ctx):
    """Everything an analyst would pull before forming a view - and nothing else.
    Sensitive vendor fields (bank account, tax ID) are deliberately excluded."""
    inv, pay, gl = ctx["invoices"], ctx["payments"], ctx["gl_entries"]
    pack = {
        "exception": {
            "exception_id": exc["exception_id"],
            "control_id": exc["control_id"],
            "control_name": exc["control_name"],
            "exception_type": exc["exception_type"],
            "severity": exc["severity"],
            "entity": exc["entity"],
            "period": exc["period"],
            "exception_amount_usd": float(exc["exception_amount_usd"]),
            "detection_rule": exc["detection_rule"],
            "evidence_statement": exc["evidence"],
            "age_days": int(exc["age_days"]),
            "assigned_queue": exc["assigned_queue"],
        },
        "policy": {
            "payment_tolerance": "greater of USD 5.00 or 0.5% of invoice",
            "materiality_review_threshold_usd": MATERIALITY_REVIEW_USD,
            "write_off_ceiling_usd": WRITE_OFF_CEILING_USD,
            "permitted_actions": ACTION_POLICY.get(exc["exception_type"], []),
        },
    }
    if exc.get("vendor_id") and not pd.isna(exc.get("vendor_id")):
        pack["vendor"] = pick(ctx["vendors"], "vendor_id", exc["vendor_id"],
                              ["vendor_id", "vendor_name", "vendor_category", "country",
                               "currency", "payment_terms", "vendor_status", "risk_tier"])
    if exc.get("invoice_id") and not pd.isna(exc.get("invoice_id")):
        pack["invoice"] = pick(inv, "invoice_id", exc["invoice_id"],
                               ["invoice_id", "invoice_number", "vendor_id", "entity",
                                "po_number", "invoice_date", "due_date", "currency",
                                "invoice_amount", "fx_rate", "invoice_amount_usd",
                                "invoice_status", "gl_account", "cost_center"])
        pack["payments_against_invoice"] = pick(
            pay, "invoice_id", exc["invoice_id"],
            ["payment_id", "payment_reference", "payment_date", "payment_method",
             "payment_amount", "payment_amount_usd", "payment_status"])
    if exc.get("payment_id") and not pd.isna(exc.get("payment_id")):
        pack["payment"] = pick(pay, "payment_id", exc["payment_id"],
                               ["payment_id", "payment_reference", "vendor_id",
                                "invoice_id", "payment_date", "payment_method",
                                "payment_amount", "payment_amount_usd",
                                "payment_status", "payment_run_id"])
    if exc.get("je_id") and not pd.isna(exc.get("je_id")):
        pack["journal_lines"] = pick(gl, "je_id", exc["je_id"],
                                     ["je_id", "je_line", "posting_date", "entity",
                                      "gl_account", "cost_center", "debit_usd",
                                      "credit_usd", "source_module", "source_doc_id",
                                      "je_type", "description", "created_by",
                                      "approved_by"])
    if exc.get("bank_reference") and not pd.isna(exc.get("bank_reference")):
        pack["bank_transaction"] = pick(
            ctx["bank_transactions"], "bank_reference", exc["bank_reference"],
            ["bank_txn_id", "bank_account", "value_date", "amount_usd", "direction",
             "transaction_type", "description", "statement_id"])
    if exc["exception_type"] == "GL_SUBLEDGER_VARIANCE":
        pack["tieout"] = ctx["tieout"][
            ctx["tieout"]["entity"] == exc["entity"]].to_dict("records")
    return pack


def pack_hash(pack):
    return hashlib.sha256(
        json.dumps(pack, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------- deterministic baseline

BASELINE = {
    "DUPLICATE_INVOICE": dict(
        action="Reverse duplicate accrual", risk="High", conf=0.88, recov=1.0,
        cause=("A second record for the same vendor invoice was keyed after the "
               "original, so the payable is recognised twice."),
        gap="No uniqueness check on normalised vendor invoice number at entry.",
        steps=["Compare both invoice images and purchase orders line by line",
               "Confirm only one goods or service receipt exists",
               "Reverse the later accrual and block the duplicate from payment",
               "If already paid, open a vendor recovery claim"]),
    "DUPLICATE_PAYMENT": dict(
        action="Recover from vendor", risk="High", conf=0.9, recov=1.0,
        cause=("The invoice was settled twice - a later payment run picked up an "
               "item already cleared."),
        gap="Payment proposal does not exclude invoices with a cleared payment.",
        steps=["Confirm both disbursements cleared the bank",
               "Contact the vendor for refund or credit memo",
               "Apply the credit against the next payment run",
               "Add a pre-release duplicate check to the payment proposal"]),
    "UNAPPLIED_PAYMENT": dict(
        action="Apply cash to open invoice", risk="Medium", conf=0.74, recov=0.35,
        cause=("Cash was released without an invoice application and is sitting in "
               "suspense rather than relieving a payable."),
        gap="Disbursement can be released without a matched invoice reference.",
        steps=["Obtain the remittance advice from the payment run",
               "Match the amount against the vendor's open items",
               "Apply the cash or request a refund if no payable exists",
               "Clear the suspense account balance"]),
    "MISSING_GL_POSTING": dict(
        action="Post correcting journal", risk="High", conf=0.86, recov=0.0,
        cause=("The sub-ledger holds a payable with no accrual journal, so the GL "
               "AP control is understated by the invoice value."),
        gap="No completeness reconciliation on the nightly sub-ledger interface.",
        steps=["Pull the interface log for the posting date and find the failure",
               "Re-post the accrual in the current open period",
               "Confirm the AP control account ties after re-posting",
               "Add a daily document-count control on the interface"]),
    "PAYMENT_AMOUNT_MISMATCH": dict(
        action="Request credit memo", risk="Medium", conf=0.8, recov=0.6,
        cause=("The payment does not agree to the invoice, and no credit memo or "
               "approved deduction supports the difference."),
        gap="Payments can be released at an amount other than the matched invoice.",
        steps=["Identify whether the difference is a dispute, discount or keying error",
               "Request the supporting credit memo from the vendor",
               "Recover an overpayment or schedule the balance of a short payment",
               "Document the approved deduction reason on the invoice"]),
    "GL_CASH_NOT_ON_STATEMENT": dict(
        action="Confirm with bank", risk="Medium", conf=0.78, recov=0.0,
        cause=("Cash was credited in the GL but the bank never cleared the item, "
               "so the reconciliation carries an unsupported outflow."),
        gap="No ageing control on issued-but-uncleared disbursements.",
        steps=["Confirm with the bank whether the payment was ever presented",
               "Check for a stale-dated or returned item",
               "Void and reissue if the vendor never received funds",
               "Age uncleared payments weekly and escalate beyond 30 days"]),
    "BANK_ITEM_NOT_IN_GL": dict(
        action="Post correcting journal", risk="Low", conf=0.82, recov=0.0,
        cause=("A bank-originated item - fee, FX charge, returned item or interest - "
               "was never journalised, so cash in the GL does not agree to the bank."),
        gap="Bank-originated items are not auto-journalised from the statement feed.",
        steps=["Classify the item from the statement narrative",
               "Post it to the correct expense or income account",
               "Confirm the cash account then agrees to the statement",
               "Map recurring bank item types to automatic postings"]),
    "UNBALANCED_JOURNAL": dict(
        action="Post correcting journal", risk="High", conf=0.92, recov=0.0,
        cause=("The journal posted with debits not equal to credits, which points "
               "to a truncated or partially loaded upload file."),
        gap="Journal upload accepts a file that does not balance.",
        steps=["Retrieve the source upload file and compare the row count",
               "Identify the missing or truncated lines",
               "Post the balancing lines in the open period",
               "Add a balance check that rejects the file before posting"]),
    "SOD_VIOLATION": dict(
        action="Obtain approval evidence", risk="High", conf=0.95, recov=0.0,
        cause=("The journal was prepared and approved by the same user, so no "
               "independent review stands behind the entry."),
        gap="Workflow does not enforce a second approver on manual journals.",
        steps=["Obtain independent review evidence for the entry after the fact",
               "Confirm the posting is supported and correctly valued",
               "Report the breach in the control log for the period",
               "Configure the workflow to block self-approval"]),
    "FX_RATE_ERROR": dict(
        action="Post correcting journal", risk="Medium", conf=0.84, recov=0.0,
        cause=("The foreign-currency payable was translated at a rate other than "
               "the published period rate, misstating the USD payable."),
        gap="Manual rate override is permitted at invoice entry.",
        steps=["Confirm the published monthly average rate for the invoice period",
               "Recompute the USD payable and quantify the difference",
               "Post the translation correction to FX gain or loss",
               "Remove rate-override rights from the AP entry role"]),
    "TOPSIDE_AP_ENTRY": dict(
        action="Obtain approval evidence", risk="High", conf=0.87, recov=0.0,
        cause=("A manual close adjustment moved the AP control account with no "
               "sub-ledger document, so the account can never tie out."),
        gap="AP control account is not restricted to sub-ledger postings.",
        steps=["Obtain the close support and approval behind the adjustment",
               "Reclassify the entry to accrued liabilities if no payable exists",
               "Reverse it in the following period if it was a timing plug",
               "Restrict manual posting rights on the AP control account"]),
    "GL_SUBLEDGER_VARIANCE": dict(
        action="Post correcting journal", risk="High", conf=0.8, recov=0.0,
        cause=("The AP control account does not agree to the sub-ledger. The "
               "variance decomposes into missing postings, rate errors and topside "
               "entries already raised as individual exceptions."),
        gap="No monthly tie-out control with a documented variance decomposition.",
        steps=["Work the decomposition back to the individual exceptions",
               "Clear the underlying items in the open period",
               "Re-run the tie-out and confirm the residual is zero",
               "Make the tie-out a mandatory close task with sign-off"]),
    "DUPLICATE_VENDOR_MASTER": dict(
        action="Merge vendor master records", risk="High", conf=0.9, recov=0.0,
        cause=("Two vendor records share a bank account and tax ID, which lets the "
               "same invoice be paid twice under different vendor numbers."),
        gap="No duplicate check on bank account or tax ID at vendor creation.",
        steps=["Confirm the records are the same legal entity",
               "Block the newer record and redirect open items to the survivor",
               "Review payment history on both records for duplicates",
               "Enable duplicate detection on bank and tax ID at creation"]),
    "AGED_OPEN_ITEM": dict(
        action="Escalate to controller", risk="Low", conf=0.7, recov=0.0,
        cause=("The payable has been open well past its due date, typically an "
               "unresolved dispute or a missing approval blocking settlement."),
        gap="No escalation path on payables beyond 90 days past due.",
        steps=["Confirm with the business owner whether the liability still stands",
               "Obtain approval to pay or documentation to release the accrual",
               "Settle or release the balance in the current period",
               "Add a 90-day escalation to the aged payables review"]),
}


def baseline_analysis(exc, pack):
    t = exc["exception_type"]
    b = BASELINE[t]
    amt = float(exc["exception_amount_usd"])
    conf = b["conf"]
    # thin evidence lowers confidence, exactly as the prompt asks of the model
    if len(pack) <= 2:
        conf = round(max(0.4, conf - 0.15), 2)
    action = b["action"]
    if amt <= WRITE_OFF_CEILING_USD and "Write off below threshold" in \
            ACTION_POLICY.get(t, []):
        action = "Write off below threshold"
    elif exc["severity"] == "Critical" and "Escalate to controller" in \
            ACTION_POLICY.get(t, []):
        action = "Escalate to controller"
    cited = [f"{exc['control_id']} {exc['exception_type']} on {exc['entity']}",
             exc["evidence"][:200]]
    return {
        "exception_id": exc["exception_id"],
        "root_cause_hypothesis": b["cause"],
        "confidence": conf,
        "evidence_cited": cited,
        "investigation_steps": b["steps"],
        "recommended_action": action,
        "recovery_expected_usd": round(amt * b["recov"], 2),
        "repeat_risk": b["risk"],
        "control_gap": b["gap"],
        "requires_human_review": bool(amt >= MATERIALITY_REVIEW_USD or
                                      exc["severity"] in ("Critical", "High")),
    }


# ------------------------------------------------------------ claude provider

def claude_analysis(exc, pack, client):
    """Same schema, produced by the model under a structured-output constraint."""
    user = (
        "Triage this reconciliation exception.\n\n"
        f"EVIDENCE PACK\n{json.dumps(pack, indent=2, default=str)}\n\n"
        f"Permitted recommended_action values for this exception type:\n"
        f"{json.dumps(ACTION_POLICY.get(exc['exception_type'], RECOMMENDED_ACTIONS))}\n\n"
        f"Return the analysis for exception_id {exc['exception_id']}."
    )
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {getattr(resp.stop_details, 'category', None)}")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# ------------------------------------------------------------- validation

NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")
ID_RE = re.compile(r"\b(?:INV-\d+|PAY-\d+|JE-\d{4}-\d+|BT-\d+|V\d{3,4}|EXC-\d+)\b")


def collect_numbers(obj, acc):
    if isinstance(obj, dict):
        for v in obj.values():
            collect_numbers(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_numbers(v, acc)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        acc.add(round(float(obj), 2))
    elif isinstance(obj, str):
        for m in NUM_RE.findall(obj):
            try:
                acc.add(round(float(m.replace(",", "")), 2))
            except ValueError:
                pass
    return acc


def collect_ids(obj, acc):
    if isinstance(obj, dict):
        for v in obj.values():
            collect_ids(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_ids(v, acc)
    elif isinstance(obj, str):
        acc.update(ID_RE.findall(obj))
    return acc


def validate(analysis, exc, pack, baseline):
    """Run V-01..V-09. Returns (failed_blocking, failed_advisory, notes)."""
    blocking, advisory, notes = [], [], []
    t = exc["exception_type"]
    amt = float(exc["exception_amount_usd"])

    # V-01 schema and types
    for field, kind in [("exception_id", str), ("root_cause_hypothesis", str),
                        ("confidence", (int, float)), ("evidence_cited", list),
                        ("investigation_steps", list), ("recommended_action", str),
                        ("recovery_expected_usd", (int, float)),
                        ("repeat_risk", str), ("control_gap", str),
                        ("requires_human_review", bool)]:
        if field not in analysis or not isinstance(analysis[field], kind):
            blocking.append("V-01")
            notes.append(f"V-01 missing or mistyped field: {field}")
            break
    if "V-01" in blocking:
        return blocking, advisory, notes
    if analysis["exception_id"] != exc["exception_id"]:
        blocking.append("V-01")
        notes.append("V-01 exception_id does not match the item under analysis")
    if not (2 <= len(analysis["investigation_steps"]) <= 6):
        advisory.append("V-01")
        notes.append("V-01 investigation_steps outside the 2-6 range")

    # V-02 closed-list enums
    if analysis["recommended_action"] not in RECOMMENDED_ACTIONS:
        blocking.append("V-02")
        notes.append(f"V-02 action not in the closed list: {analysis['recommended_action']}")
    if analysis["repeat_risk"] not in REPEAT_RISK:
        blocking.append("V-02")
        notes.append(f"V-02 repeat_risk not in the closed list: {analysis['repeat_risk']}")

    # V-03 confidence bounds and floor
    c = float(analysis["confidence"])
    if not 0.0 <= c <= 1.0:
        blocking.append("V-03")
        notes.append(f"V-03 confidence out of bounds: {c}")
    elif c < CONFIDENCE_FLOOR:
        advisory.append("V-03")
        notes.append(f"V-03 confidence {c} below the {CONFIDENCE_FLOOR} floor - "
                     "routed to a human")

    # V-04 numeric grounding
    allowed = collect_numbers(pack, set())
    allowed |= {round(amt, 2), round(float(analysis["recovery_expected_usd"]), 2), c}
    stated = collect_numbers(
        {"a": analysis["root_cause_hypothesis"], "b": analysis["evidence_cited"],
         "c": analysis["investigation_steps"], "d": analysis["control_gap"]}, set())
    ungrounded = []
    for n in stated:
        if abs(n) < 100:            # small integers are prose, not figures
            continue
        if not any(abs(n - a) <= max(0.01, abs(a) * 0.01) for a in allowed):
            ungrounded.append(n)
    if ungrounded:
        blocking.append("V-04")
        notes.append("V-04 figures not traceable to the evidence pack: "
                     + ", ".join(f"{u:,.2f}" for u in sorted(ungrounded)[:5]))

    # V-05 identifier grounding
    pack_ids = collect_ids(pack, set()) | {exc["exception_id"]}
    stated_ids = collect_ids(
        {"a": analysis["root_cause_hypothesis"], "b": analysis["evidence_cited"],
         "c": analysis["investigation_steps"], "d": analysis["control_gap"]}, set())
    unknown = sorted(stated_ids - pack_ids)
    if unknown:
        blocking.append("V-05")
        notes.append("V-05 identifiers not in the evidence pack: " + ", ".join(unknown[:5]))

    # V-06 materiality gate
    must_review = amt >= MATERIALITY_REVIEW_USD or exc["severity"] in ("Critical", "High")
    if must_review and not analysis["requires_human_review"]:
        advisory.append("V-06")
        notes.append("V-06 human review forced by materiality or severity")
        analysis["requires_human_review"] = True
    if c < CONFIDENCE_FLOOR:
        analysis["requires_human_review"] = True

    # V-07 action policy matrix
    permitted = ACTION_POLICY.get(t, [])
    if permitted and analysis["recommended_action"] not in permitted:
        blocking.append("V-07")
        notes.append(f"V-07 '{analysis['recommended_action']}' is not a permitted "
                     f"action for {t}")
    if analysis["recommended_action"] == "Write off below threshold" and \
            amt > WRITE_OFF_CEILING_USD:
        blocking.append("V-07")
        notes.append(f"V-07 write-off proposed on USD {amt:,.2f}, above the "
                     f"{WRITE_OFF_CEILING_USD:,.2f} ceiling")

    # V-08 sensitive-data screen. This screens for disclosed *values*, not for
    # the words: naming the match basis ("same bank account and tax ID") is the
    # point of the finding, while restating 12-3456789 or ...4417 is a leak.
    narrative = " ".join([analysis["root_cause_hypothesis"], analysis["control_gap"],
                          " ".join(analysis["investigation_steps"])])
    low = narrative.lower()
    leak = (re.search(r"\b\d{2}-\d{7}\b", narrative)                    # tax ID format
            or re.search(r"(?:account|acct|a/c)\D{0,18}\b\d{4}\b", low)  # account number
            or re.search(r"(?:ending|last\s*4|\*{2,})\s*\d{4}\b", low))
    if leak:
        blocking.append("V-08")
        notes.append("V-08 narrative restates a sensitive identifier value: "
                     f"'{leak.group(0).strip()}'")

    # V-09 cross-check against the deterministic baseline
    br = float(baseline["recovery_expected_usd"])
    ar = float(analysis["recovery_expected_usd"])
    if ar < 0 or ar > amt * 1.01:
        blocking.append("V-09")
        notes.append(f"V-09 recovery estimate USD {ar:,.2f} exceeds the exception value")
    elif max(br, ar) > 0 and abs(ar - br) / max(br, ar, 1.0) > RECOVERY_DIVERGENCE_TOL:
        advisory.append("V-09")
        notes.append(f"V-09 recovery estimate diverges from baseline "
                     f"(AI {ar:,.2f} vs baseline {br:,.2f})")
    if analysis["recommended_action"] != baseline["recommended_action"]:
        advisory.append("V-09")
        notes.append(f"V-09 action differs from baseline "
                     f"('{analysis['recommended_action']}' vs "
                     f"'{baseline['recommended_action']}')")

    return sorted(set(blocking)), sorted(set(advisory)), notes



# ------------------------------------------------------------- self-test

def selftest():
    """Feed deliberately defective analyses through the gate and confirm the
    expected check fires on each. A control nobody has tested is not a control.

    Writes output/validation_selftest.csv.
    """
    ctx = load_context()
    exdf = ctx["exceptions"]
    # a high-value duplicate payment gives every check something to bite on
    exc = exdf[exdf["exception_type"] == "DUPLICATE_PAYMENT"] \
        .sort_values("exception_amount_usd", ascending=False).iloc[0].to_dict()
    pack = build_evidence_pack(exc, ctx)
    base = baseline_analysis(exc, pack)

    def mutate(**kw):
        a = dict(base)
        a.update(kw)
        return a

    cases = [
        ("V-01", "required field removed",
         {k: v for k, v in base.items() if k != "control_gap"}),
        ("V-01", "confidence sent as a string", mutate(confidence="high")),
        ("V-02", "action outside the closed list",
         mutate(recommended_action="Ask the vendor nicely")),
        ("V-02", "repeat_risk outside the closed list", mutate(repeat_risk="Severe")),
        ("V-03", "confidence out of bounds", mutate(confidence=1.8)),
        ("V-03", "confidence below the floor", mutate(confidence=0.31)),
        ("V-04", "amount that is not in the evidence pack",
         mutate(root_cause_hypothesis="The vendor was overpaid by USD 482,119.00 "
                                      "across two runs.")),
        ("V-05", "document ID that does not exist",
         mutate(control_gap="The same break appears on INV-999999 and PAY-888888.")),
        ("V-06", "human review dropped on a material item",
         mutate(requires_human_review=False)),
        ("V-07", "action not permitted for this exception type",
         mutate(recommended_action="Merge vendor master records")),
        ("V-07", "write-off above the ceiling",
         mutate(recommended_action="Write off below threshold")),
        ("V-08", "bank account value restated",
         mutate(control_gap="Both records point at bank account 4417.")),
        ("V-08", "tax ID value restated",
         mutate(control_gap="Vendor tax ID 12-3456789 is shared across records.")),
        ("V-09", "recovery estimate above the exception value",
         mutate(recovery_expected_usd=float(exc["exception_amount_usd"]) * 3)),
        ("V-09", "action diverges from the deterministic baseline",
         mutate(recommended_action="Void and reissue payment")),
    ]

    rows, failures = [], 0
    for expected, label, analysis in cases:
        blocking, advisory, notes = validate(dict(analysis), exc, pack, base)
        fired = set(blocking) | set(advisory)
        ok = expected in fired
        if not ok:
            failures += 1
        rows.append({"expected_check": expected, "scenario": label,
                     "checks_fired": ",".join(sorted(fired)),
                     "blocking": ",".join(blocking), "advisory": ",".join(advisory),
                     "result": "pass" if ok else "FAIL",
                     "detail": "; ".join(notes)[:160]})

    # V-10: the same evidence pack must hash identically every time
    h1, h2 = pack_hash(pack), pack_hash(build_evidence_pack(exc, ctx))
    rows.append({"expected_check": "V-10", "scenario": "evidence pack hash is stable",
                 "checks_fired": "", "blocking": "", "advisory": "",
                 "result": "pass" if h1 == h2 else "FAIL",
                 "detail": f"{h1[:16]} == {h2[:16]}"})
    if h1 != h2:
        failures += 1

    # and a clean baseline analysis must pass the whole gate untouched
    blocking, advisory, _ = validate(dict(base), exc, pack, base)
    clean_ok = not blocking and not advisory
    rows.append({"expected_check": "-", "scenario": "clean baseline analysis passes",
                 "checks_fired": ",".join(sorted(set(blocking) | set(advisory))),
                 "blocking": ",".join(blocking), "advisory": ",".join(advisory),
                 "result": "pass" if clean_ok else "FAIL", "detail": ""})
    if not clean_ok:
        failures += 1

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT, "validation_selftest.csv"), index=False)
    print(f"validation self-test on {exc['exception_id']} "
          f"({exc['exception_type']}, USD {float(exc['exception_amount_usd']):,.2f})\n")
    print(f"  {'check':<6} {'scenario':<46} {'fired':<16} result")
    for r in rows:
        print(f"  {r['expected_check']:<6} {r['scenario'][:45]:<46} "
              f"{r['checks_fired'][:15]:<16} {r['result']}")
    print(f"\n{len(rows) - failures}/{len(rows)} cases passed - "
          "wrote output/validation_selftest.csv")
    if failures:
        sys.exit(1)


# ------------------------------------------------------------------- runner

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["rules", "claude"], default="rules",
                    help="rules = deterministic baseline (default, offline); "
                         "claude = Claude API with the schema enforced")
    ap.add_argument("--limit", type=int, default=0,
                    help="analyse only the N highest-value exceptions "
                         "(default: all; 25 when --provider claude)")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore output/ai_cache.json and re-run the provider")
    ap.add_argument("--selftest", action="store_true",
                    help="run the negative tests that prove each validation "
                         "check fires, then exit")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    random.seed(99)
    ctx = load_context()
    exdf = ctx["exceptions"].sort_values("exception_amount_usd", ascending=False)
    limit = args.limit or (25 if args.provider == "claude" else 0)
    if limit:
        exdf = exdf.head(limit)

    client = None
    if args.provider == "claude":
        try:
            import anthropic
        except ImportError:
            sys.exit("pip install anthropic to use --provider claude")
        client = anthropic.Anthropic()
        print(f"provider: Claude API ({MODEL}), structured output + server-side "
              f"refusal fallback enabled")
    else:
        print("provider: deterministic baseline analyser (no API calls)")

    cache = {}
    if os.path.exists(CACHE_PATH) and not args.no_cache:
        cache = json.load(open(CACHE_PATH, encoding="utf-8"))

    records, logs = [], []
    for _, exc in exdf.iterrows():
        exc = exc.to_dict()
        pack = build_evidence_pack(exc, ctx)
        h = pack_hash(pack)
        baseline = baseline_analysis(exc, pack)

        source, cached = args.provider, False
        if args.provider == "claude":
            key = f"{MODEL}:{h}"
            if key in cache:                                   # V-10
                analysis, cached = cache[key], True
            else:
                try:
                    analysis = claude_analysis(exc, pack, client)
                    cache[key] = analysis
                except Exception as e:                         # fail safe, never fail open
                    logs.append({
                        "exception_id": exc["exception_id"], "provider": "claude",
                        "evidence_hash": h, "cached": False,
                        "validation_status": "Provider error - baseline used",
                        "blocking_checks": "", "advisory_checks": "",
                        "notes": f"provider error: {e}",
                        "logged_at": datetime.now().isoformat(timespec="seconds")})
                    analysis, source = baseline, "rules (fallback)"
        else:
            analysis = dict(baseline)

        blocking, advisory, notes = validate(analysis, exc, pack, baseline)
        if blocking:
            status = "Rejected - baseline used"
            final = dict(baseline)
            final["requires_human_review"] = True
        elif advisory:
            status = "Accepted with flags"
            final = analysis
        else:
            status = "Accepted"
            final = analysis

        # simulated human disposition, deterministic per exception
        rng = random.Random(int(hashlib.md5(exc["exception_id"].encode()).hexdigest()[:8], 16))
        if exc["exception_status"] == "Resolved":
            roll = rng.random()
            disposition = ("Accepted" if roll < 0.68 else
                           "Accepted with edits" if roll < 0.9 else "Overridden")
        else:
            disposition = "Pending review" if final["requires_human_review"] \
                else "Auto-routed to queue"

        records.append({
            "exception_id": exc["exception_id"],
            "control_id": exc["control_id"],
            "exception_type": exc["exception_type"],
            "severity": exc["severity"],
            "entity": exc["entity"],
            "exception_amount_usd": round(float(exc["exception_amount_usd"]), 2),
            "exception_status": exc["exception_status"],
            "assigned_queue": exc["assigned_queue"],
            "analysis_source": source,
            "from_cache": cached,
            "root_cause_hypothesis": final["root_cause_hypothesis"],
            "confidence": round(float(final["confidence"]), 2),
            "recommended_action": final["recommended_action"],
            "recovery_expected_usd": round(float(final["recovery_expected_usd"]), 2),
            "repeat_risk": final["repeat_risk"],
            "control_gap": final["control_gap"],
            "investigation_steps": " | ".join(final["investigation_steps"]),
            "evidence_cited": " | ".join(str(x) for x in final["evidence_cited"]),
            "requires_human_review": bool(final["requires_human_review"]),
            "validation_status": status,
            "blocking_checks": ",".join(blocking),
            "advisory_checks": ",".join(advisory),
            "reviewer_disposition": disposition,
            "evidence_hash": h,
        })
        logs.append({
            "exception_id": exc["exception_id"], "provider": source,
            "evidence_hash": h, "cached": cached, "validation_status": status,
            "blocking_checks": ",".join(blocking), "advisory_checks": ",".join(advisory),
            "notes": "; ".join(notes),
            "logged_at": datetime.now().isoformat(timespec="seconds"),
        })

    rdf = pd.DataFrame(records)
    rdf.to_csv(os.path.join(OUT, "exception_analysis.csv"), index=False)
    pd.DataFrame(logs).to_csv(os.path.join(OUT, "ai_validation_log.csv"), index=False)
    if args.provider == "claude":
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)

    checks = {}
    for lg in logs:
        for c in (lg["blocking_checks"].split(",") + lg["advisory_checks"].split(",")):
            if c:
                checks[c] = checks.get(c, 0) + 1
    reviewed = rdf[rdf["reviewer_disposition"].isin(
        ["Accepted", "Accepted with edits", "Overridden"])]
    summary = {
        "provider": args.provider,
        "model": MODEL if args.provider == "claude" else "deterministic baseline",
        "analysed": int(len(rdf)),
        "accepted": int((rdf["validation_status"] == "Accepted").sum()),
        "accepted_with_flags": int((rdf["validation_status"] == "Accepted with flags").sum()),
        "rejected_to_baseline": int(rdf["validation_status"].str.startswith("Rejected").sum()),
        "validation_pass_rate_pct": round(
            100 * (~rdf["validation_status"].str.startswith("Rejected")).mean(), 1),
        "avg_confidence": round(float(rdf["confidence"].mean()), 3),
        "requires_human_review": int(rdf["requires_human_review"].sum()),
        "human_review_pct": round(100 * float(rdf["requires_human_review"].mean()), 1),
        "auto_routable_pct": round(100 * (1 - float(rdf["requires_human_review"].mean())), 1),
        "expected_recovery_usd": round(float(rdf["recovery_expected_usd"].sum()), 2),
        "reviewer_acceptance_pct": round(
            100 * float(reviewed["reviewer_disposition"].isin(
                ["Accepted", "Accepted with edits"]).mean()), 1) if len(reviewed) else None,
        "reviewer_override_pct": round(
            100 * float((reviewed["reviewer_disposition"] == "Overridden").mean()), 1)
        if len(reviewed) else None,
        "validation_checks_triggered": dict(sorted(checks.items())),
        "manual_triage_hours_baseline": round(len(rdf) * MANUAL_TRIAGE_MINUTES / 60, 1),
        "by_recommended_action": rdf.groupby("recommended_action")
            .agg(count=("exception_id", "count"),
                 value_usd=("exception_amount_usd", "sum")).round(2)
            .reset_index().sort_values("value_usd", ascending=False).to_dict("records"),
        "by_repeat_risk": rdf.groupby("repeat_risk")["exception_id"].count().to_dict(),
        "by_control_gap": rdf.groupby("control_gap")
            .agg(exceptions=("exception_id", "count"),
                 value_usd=("exception_amount_usd", "sum")).round(2)
            .reset_index().sort_values("value_usd", ascending=False).to_dict("records"),
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
    }
    with open(os.path.join(OUT, "exception_analysis.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "records": records}, f, indent=2, default=str)

    print(f"\nanalysed {summary['analysed']} exceptions")
    print(f"  validation      {summary['accepted']} accepted, "
          f"{summary['accepted_with_flags']} accepted with flags, "
          f"{summary['rejected_to_baseline']} rejected to baseline "
          f"({summary['validation_pass_rate_pct']}% pass rate)")
    print(f"  routing         {summary['requires_human_review']} require human review "
          f"({summary['human_review_pct']}%), "
          f"{summary['auto_routable_pct']}% auto-routable")
    print(f"  avg confidence  {summary['avg_confidence']}")
    print(f"  expected recovery USD {summary['expected_recovery_usd']:,.2f}")
    if summary["validation_checks_triggered"]:
        print("  checks triggered: " + ", ".join(
            f"{k} x{v}" for k, v in summary["validation_checks_triggered"].items()))
    print("  wrote output/exception_analysis.csv, exception_analysis.json, "
          "ai_validation_log.csv")


if __name__ == "__main__":
    main()
