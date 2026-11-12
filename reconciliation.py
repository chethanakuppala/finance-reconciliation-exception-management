#!/usr/bin/env python3
"""
reconciliation.py - the reconciliation and exception-detection engine.

Reads the four simulated source systems in data/ and runs 13 controls,
which raise 14 exception types:

  C-01  Duplicate invoice (normalised number + amount/date proximity)
  C-02  Duplicate payment against an already-settled invoice
  C-03  Unapplied / on-account payment with no invoice reference
  C-04  AP sub-ledger invoice with no GL accrual (interface drop)
  C-05  Payment amount vs invoice amount beyond tolerance
  C-06  Bank reconciliation breaks, both directions
  C-07  Journal entry where debits != credits
  C-08  Segregation-of-duties breach on a manual journal
  C-09  FX translation rate not equal to the published period rate
  C-10  Topside manual entry to the AP control account
  C-11  GL AP control vs AP sub-ledger balance tie-out (derived)
  C-13  Aged open item past 90 days overdue (operational)
  C-12  Duplicate vendor master record
  (C-11 and C-13 are derived/operational controls with no seeded population.)

Outputs
  output/exceptions.csv          one row per exception, with workflow state
  output/invoice_aging.csv       AP aging by vendor and bucket
  output/gl_vs_subledger.csv     AP control tie-out by entity, with variance
                                 decomposed back to the controls that caused it
  output/detection_scorecard.csv recall / precision per control vs the seeded
                                 defect log (data/seeded_defects.csv)
  output/recon_output.json       every figure the dashboard and Power BI use

The seeded defect log is read ONLY for the scorecard, after detection is
complete. No control rule touches it.
"""

import json
import os
import re
import random
import hashlib
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")

AS_OF = date(2026, 9, 30)
AP_CONTROL = "20100"
CASH_ACCOUNT = "10100"

# ------------------------------------------------------------- control config
TOL_ABS = 5.00          # payment-vs-invoice absolute tolerance, USD
TOL_PCT = 0.005         # payment-vs-invoice relative tolerance
TOL_JE = 0.05           # journal balance tolerance (cent rounding on FX)
TOL_FX_ABS = 25.00      # FX translation tolerance, USD
TOL_FX_PCT = 0.01
TOL_TIEOUT = 1000.00    # GL vs sub-ledger reporting threshold, USD
DUP_INV_WINDOW = 10     # days, amount/date proximity rule
DUP_PMT_WINDOW = 7      # days
AGED_DAYS = 90          # operational aged-item threshold

# severity ladder (USD)
SEV_CRITICAL = 75_000
SEV_HIGH = 25_000
SEV_MEDIUM = 5_000

CONTROL_NAMES = {
    "C-01": "Duplicate invoice detection",
    "C-02": "Duplicate payment detection",
    "C-03": "Unapplied cash / payment without invoice",
    "C-04": "AP sub-ledger to GL completeness",
    "C-05": "Payment to invoice amount agreement",
    "C-06": "Bank statement to GL cash reconciliation",
    "C-07": "Journal entry debit/credit balance",
    "C-08": "Segregation of duties on manual journals",
    "C-09": "FX translation rate accuracy",
    "C-10": "Topside entries to AP control account",
    "C-11": "AP control account tie-out (GL vs sub-ledger)",
    "C-12": "Vendor master duplicate detection",
    "C-13": "Aged open payable review",
}

QUEUE = {
    "C-01": "AP Operations", "C-02": "AP Operations", "C-03": "AP Operations",
    "C-04": "GL & Close Team", "C-05": "AP Operations",
    "C-06": "Treasury & Cash Operations", "C-07": "GL & Close Team",
    "C-08": "Controllership Review", "C-09": "GL & Close Team",
    "C-10": "Controllership Review", "C-11": "GL & Close Team",
    "C-12": "Vendor Master Data", "C-13": "AP Operations",
}

ROOT_CAUSE = {
    "DUPLICATE_INVOICE": "Manual re-keying / no invoice-number uniqueness check",
    "DUPLICATE_PAYMENT": "Payment run picked up an already-settled item",
    "UNAPPLIED_PAYMENT": "Disbursement released without invoice application",
    "MISSING_GL_POSTING": "Sub-ledger to GL interface drop",
    "PAYMENT_AMOUNT_MISMATCH": "Short pay / overpay not matched to a credit memo",
    "GL_CASH_NOT_ON_STATEMENT": "Payment issued but not cleared by the bank",
    "BANK_ITEM_NOT_IN_GL": "Bank-originated item never journalised",
    "UNBALANCED_JOURNAL": "Partial or truncated journal upload",
    "SOD_VIOLATION": "Preparer and approver are the same user",
    "FX_RATE_ERROR": "Manual rate override instead of the published period rate",
    "TOPSIDE_AP_ENTRY": "Close adjustment booked without sub-ledger support",
    "GL_SUBLEDGER_VARIANCE": "Aggregate effect of upstream posting exceptions",
    "DUPLICATE_VENDOR_MASTER": "Vendor created without duplicate-check on bank/tax ID",
    "AGED_OPEN_ITEM": "Unresolved dispute or missing approval blocking settlement",
}

SLA_DAYS = {
    "Critical": 3, "High": 5, "Medium": 10, "Low": 20,
}

# ----------------------------------------------------------------- data load

def load():
    d = {}
    for name in ["vendors", "invoices", "payments", "gl_entries",
                 "bank_transactions", "fx_rates", "chart_of_accounts",
                 "cost_centers", "entities"]:
        df = pd.read_csv(os.path.join(DATA, f"{name}.csv"),
                         dtype={"gl_account": str, "bank_account_last4": str,
                                "invoice_number": str})
        d[name] = df
    inv = d["invoices"]
    for c in ["invoice_date", "received_date", "due_date"]:
        inv[c] = pd.to_datetime(inv[c])
    pay = d["payments"]
    pay["payment_date"] = pd.to_datetime(pay["payment_date"])
    pay["invoice_id"] = pay["invoice_id"].fillna("").astype(str)
    gl = d["gl_entries"]
    gl["posting_date"] = pd.to_datetime(gl["posting_date"])
    gl["gl_account"] = gl["gl_account"].astype(str)
    bank = d["bank_transactions"]
    bank["value_date"] = pd.to_datetime(bank["value_date"])
    return d


def norm_invoice_no(x):
    """Strip the cosmetic differences AP systems treat as distinct numbers."""
    return re.sub(r"[^A-Z0-9]", "", str(x).strip().upper())


def norm_name(x):
    return re.sub(r"[^A-Z0-9]", "", str(x).strip().upper())


def ex(control_id, exception_type, *, entity, source_date, amount, rule, evidence,
       key_type, key, vendor_id="", invoice_id="", payment_id="", je_id="",
       bank_reference=""):
    return {
        "control_id": control_id,
        "control_name": CONTROL_NAMES[control_id],
        "exception_type": exception_type,
        "entity": entity,
        "source_date": pd.Timestamp(source_date),
        "exception_amount_usd": round(float(amount), 2),
        "detection_rule": rule,
        "evidence": evidence,
        "match_key_type": key_type,
        "match_key": key,
        "vendor_id": vendor_id,
        "invoice_id": invoice_id,
        "payment_id": payment_id,
        "je_id": je_id,
        "bank_reference": bank_reference,
        "root_cause_category": ROOT_CAUSE[exception_type],
    }


# --------------------------------------------------------------- the controls

def c01_duplicate_invoices(inv):
    """Same vendor + identical normalised invoice number, OR same vendor +
    identical amount within a 10-day window. The later-received record is the
    exception; the earlier one is the original."""
    out = []
    w = inv.copy()
    w["norm_no"] = w["invoice_number"].map(norm_invoice_no)
    flagged = set()

    for (vid, nno), g in w.groupby(["vendor_id", "norm_no"]):
        if len(g) < 2:
            continue
        g = g.sort_values(["received_date", "invoice_id"])
        first = g.iloc[0]
        for _, r in g.iloc[1:].iterrows():
            flagged.add(r["invoice_id"])
            out.append(ex("C-01", "DUPLICATE_INVOICE", entity=r["entity"],
                          source_date=r["received_date"], amount=r["invoice_amount_usd"],
                          rule="Exact match on vendor + normalised invoice number",
                          evidence=(f"{r['invoice_id']} ({r['invoice_number']}) repeats "
                                    f"{first['invoice_id']} ({first['invoice_number']}) "
                                    f"for {vid}; both {r['currency']} "
                                    f"{r['invoice_amount']:,.2f}"),
                          key_type="invoice_id", key=r["invoice_id"],
                          vendor_id=vid, invoice_id=r["invoice_id"]))

    for (vid, amt), g in w.groupby(["vendor_id", "invoice_amount"]):
        if len(g) < 2:
            continue
        g = g.sort_values(["received_date", "invoice_id"])
        rows = list(g.itertuples())
        for i in range(1, len(rows)):
            r, first = rows[i], rows[i - 1]
            gap = (r.received_date - first.received_date).days
            if gap > DUP_INV_WINDOW or r.invoice_id in flagged:
                continue
            flagged.add(r.invoice_id)
            out.append(ex("C-01", "DUPLICATE_INVOICE", entity=r.entity,
                          source_date=r.received_date, amount=r.invoice_amount_usd,
                          rule=f"Vendor + identical amount within {DUP_INV_WINDOW} days",
                          evidence=(f"{r.invoice_id} and {first.invoice_id} both bill {vid} "
                                    f"{r.currency} {amt:,.2f}, {gap} day(s) apart"),
                          key_type="invoice_id", key=r.invoice_id,
                          vendor_id=vid, invoice_id=r.invoice_id))
    return out


def c02_duplicate_payments(pay, inv):
    """More than one payment applied to the same invoice, or the same vendor
    paid an identical amount twice inside a 7-day window."""
    out = []
    imap = inv.set_index("invoice_id")
    flagged = set()
    applied = pay[pay["invoice_id"] != ""]

    for iid, g in applied.groupby("invoice_id"):
        if len(g) < 2 or iid not in imap.index:
            continue
        inv_row = imap.loc[iid]
        if inv_row["invoice_status"] == "Partially Paid":
            continue                      # instalments are expected, not breaks
        g = g.sort_values(["payment_date", "payment_id"])
        first = g.iloc[0]
        for _, r in g.iloc[1:].iterrows():
            flagged.add(r["payment_id"])
            out.append(ex("C-02", "DUPLICATE_PAYMENT", entity=r["entity"],
                          source_date=r["payment_date"], amount=r["payment_amount_usd"],
                          rule="Second payment applied to an already-settled invoice",
                          evidence=(f"{r['payment_id']} ({r['payment_reference']}) pays "
                                    f"{iid} again - already settled by {first['payment_id']} "
                                    f"on {first['payment_date'].date()}"),
                          key_type="payment_id", key=r["payment_id"],
                          vendor_id=r["vendor_id"], invoice_id=iid,
                          payment_id=r["payment_id"]))

    w = pay.copy()
    w["amt_key"] = w["payment_amount_usd"].round(2)
    for (vid, amt), g in w.groupby(["vendor_id", "amt_key"]):
        if len(g) < 2:
            continue
        g = g.sort_values(["payment_date", "payment_id"])
        rows = list(g.itertuples())
        for i in range(1, len(rows)):
            r, first = rows[i], rows[i - 1]
            if r.payment_id in flagged:
                continue
            if (r.payment_date - first.payment_date).days > DUP_PMT_WINDOW:
                continue
            flagged.add(r.payment_id)
            out.append(ex("C-02", "DUPLICATE_PAYMENT", entity=r.entity,
                          source_date=r.payment_date, amount=r.payment_amount_usd,
                          rule=f"Vendor + identical USD amount within {DUP_PMT_WINDOW} days",
                          evidence=(f"{r.payment_id} and {first.payment_id} both pay {vid} "
                                    f"USD {amt:,.2f}"),
                          key_type="payment_id", key=r.payment_id,
                          vendor_id=vid, invoice_id=r.invoice_id,
                          payment_id=r.payment_id))
    return out


def c03_unapplied_payments(pay, inv):
    """Cash left the bank with no invoice applied, or pointing at an invoice
    that does not exist in the sub-ledger."""
    ids = set(inv["invoice_id"])
    out = []
    for r in pay.itertuples():
        orphan = (r.invoice_id == "") or (r.invoice_id not in ids)
        if not orphan:
            continue
        why = ("no invoice reference on the payment record" if r.invoice_id == ""
               else f"references {r.invoice_id}, which is not in the AP sub-ledger")
        out.append(ex("C-03", "UNAPPLIED_PAYMENT", entity=r.entity,
                      source_date=r.payment_date, amount=r.payment_amount_usd,
                      rule="Payment with no resolvable invoice application",
                      evidence=(f"{r.payment_id} ({r.payment_reference}) paid {r.vendor_id} "
                                f"USD {r.payment_amount_usd:,.2f} - {why}"),
                      key_type="payment_id", key=r.payment_id,
                      vendor_id=r.vendor_id, payment_id=r.payment_id))
    return out


def c04_missing_gl(inv, gl):
    """Every sub-ledger invoice must have an AP control credit in the GL."""
    posted = set(gl.loc[(gl["gl_account"] == AP_CONTROL) & (gl["source_module"] == "AP"),
                        "source_doc_id"])
    out = []
    for r in inv.itertuples():
        if r.invoice_id in posted:
            continue
        out.append(ex("C-04", "MISSING_GL_POSTING", entity=r.entity,
                      source_date=r.received_date, amount=r.invoice_amount_usd,
                      rule="Sub-ledger invoice with no AP control credit in the GL",
                      evidence=(f"{r.invoice_id} ({r.invoice_number}) for {r.vendor_id} sits in "
                                f"the AP sub-ledger at USD {r.invoice_amount_usd:,.2f} with no "
                                f"accrual journal - GL AP is understated by that amount"),
                      key_type="invoice_id", key=r.invoice_id,
                      vendor_id=r.vendor_id, invoice_id=r.invoice_id))
    return out


def c05_amount_mismatch(pay, inv):
    """Each applied payment must agree to its invoice within tolerance.
    Instalment (Partially Paid) invoices are out of scope by design."""
    imap = inv.set_index("invoice_id")
    out = []
    for r in pay.itertuples():
        if not r.invoice_id or r.invoice_id not in imap.index:
            continue
        iv = imap.loc[r.invoice_id]
        if iv["invoice_status"] == "Partially Paid":
            continue
        diff = round(r.payment_amount - iv["invoice_amount"], 2)
        tol = max(TOL_ABS, abs(iv["invoice_amount"]) * TOL_PCT)
        if abs(diff) <= tol:
            continue
        usd = round(abs(diff) * r.fx_rate, 2)
        kind = "Overpaid" if diff > 0 else "Short paid"
        out.append(ex("C-05", "PAYMENT_AMOUNT_MISMATCH", entity=r.entity,
                      source_date=r.payment_date, amount=usd,
                      rule=f"|payment - invoice| > max(USD {TOL_ABS:,.2f}, {TOL_PCT:.1%})",
                      evidence=(f"{kind} {iv['currency']} {abs(diff):,.2f} "
                                f"(USD {usd:,.2f}): {r.payment_id} paid "
                                f"{r.payment_amount:,.2f} against {r.invoice_id} at "
                                f"{iv['invoice_amount']:,.2f}"),
                      key_type="payment_id", key=r.payment_id,
                      vendor_id=r.vendor_id, invoice_id=r.invoice_id,
                      payment_id=r.payment_id))
    return out


def c06_bank_breaks(pay, gl, bank):
    """Two-way match between GL cash disbursements and the bank statement,
    keyed on the payment/bank reference."""
    out = []
    bank_refs = set(bank["bank_reference"])
    gl_cash_docs = set(gl.loc[gl["gl_account"] == CASH_ACCOUNT, "source_doc_id"])

    for r in pay.itertuples():
        if r.payment_id not in gl_cash_docs:
            continue                      # no GL cash movement, nothing to clear
        if r.payment_reference in bank_refs:
            continue
        days = (AS_OF - r.payment_date.date()).days
        out.append(ex("C-06", "GL_CASH_NOT_ON_STATEMENT", entity=r.entity,
                      source_date=r.payment_date, amount=r.payment_amount_usd,
                      rule="GL cash credit with no bank statement line on the reference",
                      evidence=(f"{r.payment_id} ({r.payment_reference}) credited cash "
                                f"USD {r.payment_amount_usd:,.2f} on "
                                f"{r.payment_date.date()} - uncleared {days} days "
                                f"at the statement cut-off"),
                      key_type="payment_id", key=r.payment_id,
                      vendor_id=r.vendor_id, payment_id=r.payment_id))

    pay_refs = set(pay["payment_reference"])
    for b in bank.itertuples():
        if b.bank_reference in pay_refs:
            continue
        out.append(ex("C-06", "BANK_ITEM_NOT_IN_GL", entity=b.entity,
                      source_date=b.value_date, amount=b.amount_usd,
                      rule="Bank statement line with no payment or GL document",
                      evidence=(f"{b.transaction_type} {b.direction} USD {b.amount_usd:,.2f} "
                                f"on {b.value_date.date()} ({b.bank_reference}): "
                                f"{b.description} - no GL entry"),
                      key_type="bank_reference", key=b.bank_reference,
                      bank_reference=b.bank_reference))
    return out


def c07_unbalanced_journals(gl):
    out = []
    g = gl.groupby("je_id").agg(dr=("debit_usd", "sum"), cr=("credit_usd", "sum"),
                                entity=("entity", "first"),
                                posting_date=("posting_date", "first"),
                                je_type=("je_type", "first"),
                                desc=("description", "first"),
                                created_by=("created_by", "first")).reset_index()
    g["diff"] = (g["dr"] - g["cr"]).round(2)
    for r in g[g["diff"].abs() > TOL_JE].itertuples():
        out.append(ex("C-07", "UNBALANCED_JOURNAL", entity=r.entity,
                      source_date=r.posting_date, amount=abs(r.diff),
                      rule=f"|sum(debits) - sum(credits)| > USD {TOL_JE:,.2f} at journal level",
                      evidence=(f"{r.je_id} ({r.je_type}, {r.created_by}): debits "
                                f"USD {r.dr:,.2f} vs credits USD {r.cr:,.2f}, "
                                f"out of balance by USD {r.diff:,.2f} - '{r.desc}'"),
                      key_type="je_id", key=r.je_id, je_id=r.je_id))
    return out


def c08_sod(gl):
    out = []
    g = gl.groupby("je_id").agg(dr=("debit_usd", "sum"), entity=("entity", "first"),
                                posting_date=("posting_date", "first"),
                                je_type=("je_type", "first"),
                                created_by=("created_by", "first"),
                                approved_by=("approved_by", "first"),
                                desc=("description", "first")).reset_index()
    for r in g.itertuples():
        if r.created_by != r.approved_by:
            continue
        out.append(ex("C-08", "SOD_VIOLATION", entity=r.entity,
                      source_date=r.posting_date, amount=r.dr,
                      rule="Journal where created_by equals approved_by",
                      evidence=(f"{r.je_id} ({r.je_type}) posting USD {r.dr:,.2f} was prepared "
                                f"and approved by the same user ({r.created_by}) - '{r.desc}'"),
                      key_type="je_id", key=r.je_id, je_id=r.je_id))
    return out


def c09_fx_rate(inv, gl, fx):
    """The AP credit posted for a foreign-currency invoice must equal the
    invoice amount at the published monthly average rate."""
    rates = {(r.period, r.from_currency): r.fx_rate for r in fx.itertuples()}
    ap = gl[(gl["gl_account"] == AP_CONTROL) & (gl["source_module"] == "AP")]
    posted = ap.groupby("source_doc_id")["credit_usd"].sum().to_dict()
    out = []
    for r in inv.itertuples():
        if r.currency == "USD" or r.invoice_id not in posted:
            continue
        # policy: the translation period follows the invoice date, not the
        # date the document happened to be keyed
        period = f"{r.invoice_date.year}-{r.invoice_date.month:02d}"
        pub = rates.get((period, r.currency))
        if not pub:
            continue
        expected = round(r.invoice_amount * pub, 2)
        actual = round(posted[r.invoice_id], 2)
        diff = round(actual - expected, 2)
        tol = max(TOL_FX_ABS, abs(expected) * TOL_FX_PCT)
        if abs(diff) <= tol:
            continue
        implied = round(actual / r.invoice_amount, 6) if r.invoice_amount else 0
        out.append(ex("C-09", "FX_RATE_ERROR", entity=r.entity,
                      source_date=r.received_date, amount=abs(diff),
                      rule=f"|posted AP - invoice x published rate| > max(USD "
                           f"{TOL_FX_ABS:,.0f}, {TOL_FX_PCT:.0%})",
                      evidence=(f"{r.invoice_id}: {r.currency} {r.invoice_amount:,.2f} posted at "
                                f"USD {actual:,.2f} (implied rate {implied}) vs published "
                                f"{period} rate {pub} = USD {expected:,.2f}; "
                                f"difference USD {diff:,.2f}"),
                      key_type="invoice_id", key=r.invoice_id,
                      vendor_id=r.vendor_id, invoice_id=r.invoice_id))
    return out


def c10_topside(gl):
    """Manual journal lines straight to the AP control account carry no
    sub-ledger document, so the sub-ledger can never tie to them."""
    rows = gl[(gl["gl_account"] == AP_CONTROL) & (gl["source_module"] != "AP")]
    out = []
    for je, g in rows.groupby("je_id"):
        net = round(g["credit_usd"].sum() - g["debit_usd"].sum(), 2)
        r = g.iloc[0]
        out.append(ex("C-10", "TOPSIDE_AP_ENTRY", entity=r["entity"],
                      source_date=r["posting_date"], amount=abs(net),
                      rule="AP control account movement with source_module != AP",
                      evidence=(f"{je} moved the AP control account by USD {net:,.2f} with no "
                                f"sub-ledger document ({r['created_by']} / "
                                f"{r['approved_by']}) - '{r['description']}'"),
                      key_type="je_id", key=je, je_id=je))
    return out


def c12_duplicate_vendors(vendors):
    """Same bank account + tax ID, or an identical normalised name."""
    out, flagged = [], set()
    v = vendors.copy()
    v["norm_name"] = v["vendor_name"].map(norm_name)
    v["onboarded_date"] = pd.to_datetime(v["onboarded_date"])

    for (bank, tax), g in v.groupby(["bank_account_last4", "tax_id"]):
        if len(g) < 2:
            continue
        g = g.sort_values(["onboarded_date", "vendor_id"])
        first = g.iloc[0]
        for _, r in g.iloc[1:].iterrows():
            flagged.add(r["vendor_id"])
            out.append(ex("C-12", "DUPLICATE_VENDOR_MASTER", entity="ALL",
                          source_date=r["onboarded_date"], amount=0.0,
                          rule="Shared bank account last4 + tax ID across vendor records",
                          evidence=(f"{r['vendor_id']} '{r['vendor_name']}' duplicates "
                                    f"{first['vendor_id']} '{first['vendor_name']}' - same bank "
                                    f"account ...{bank} and tax ID {tax}"),
                          key_type="vendor_id", key=r["vendor_id"],
                          vendor_id=r["vendor_id"]))

    for nn, g in v.groupby("norm_name"):
        if len(g) < 2:
            continue
        g = g.sort_values(["onboarded_date", "vendor_id"])
        first = g.iloc[0]
        for _, r in g.iloc[1:].iterrows():
            if r["vendor_id"] in flagged:
                continue
            flagged.add(r["vendor_id"])
            out.append(ex("C-12", "DUPLICATE_VENDOR_MASTER", entity="ALL",
                          source_date=r["onboarded_date"], amount=0.0,
                          rule="Identical normalised vendor name",
                          evidence=(f"{r['vendor_id']} '{r['vendor_name']}' normalises to the "
                                    f"same name as {first['vendor_id']} "
                                    f"'{first['vendor_name']}'"),
                          key_type="vendor_id", key=r["vendor_id"],
                          vendor_id=r["vendor_id"]))
    return out


def c13_aged_items(inv, pay):
    """Open payables more than 90 days past due at the close date."""
    paid = pay[pay["invoice_id"] != ""].groupby("invoice_id")["payment_amount_usd"].sum()
    out = []
    openset = inv[inv["invoice_status"].isin(["Open", "Disputed", "Partially Paid"])]
    for r in openset.itertuples():
        days = (AS_OF - r.due_date.date()).days
        if days <= AGED_DAYS:
            continue
        bal = round(r.invoice_amount_usd - float(paid.get(r.invoice_id, 0.0)), 2)
        if bal <= 0:
            continue
        out.append(ex("C-13", "AGED_OPEN_ITEM", entity=r.entity,
                      source_date=r.due_date, amount=bal,
                      rule=f"Open balance with due_date more than {AGED_DAYS} days before close",
                      evidence=(f"{r.invoice_id} ({r.vendor_id}) status {r.invoice_status}, "
                                f"USD {bal:,.2f} open {days} days past due "
                                f"({r.due_date.date()})"),
                      key_type="invoice_id", key=r.invoice_id,
                      vendor_id=r.vendor_id, invoice_id=r.invoice_id))
    return out


# ------------------------------------------------------- C-11 balance tie-out

def c11_tieout(inv, pay, gl):
    """AP control tie-out per entity.

    The variance between the GL AP control account and the AP sub-ledger open
    balance is decomposed into its four possible causes, all derived from the
    ledger itself. The residual is the proof: it should be zero, which means
    the exception register accounts for the entire balance difference.

      sub-ledger open = (invoice - payments applied) in document currency,
                        translated at the invoice-date rate
      GL AP control   = AP credits - AP debits
    """
    inv_ids = set(inv["invoice_id"])
    applied = pay[pay["invoice_id"].isin(inv_ids)]
    paid_doc = applied.groupby("invoice_id")["payment_amount"].sum()

    ap = gl[gl["gl_account"] == AP_CONTROL]
    ap_sub = ap[ap["source_module"] == "AP"]
    accrual_credit = ap_sub[ap_sub["source_doc_id"].isin(inv_ids)] \
        .groupby("source_doc_id")["credit_usd"].sum()
    posted_ids = set(accrual_credit.index)
    relief_debit = ap_sub[~ap_sub["source_doc_id"].isin(inv_ids)] \
        .groupby("source_doc_id")["debit_usd"].sum()
    pay_entity = pay.set_index("payment_id")["entity"].to_dict()

    rows = []
    for entity in sorted(set(inv["entity"])):
        e_inv = inv[inv["entity"] == entity]
        open_doc = (e_inv["invoice_amount"]
                    - e_inv["invoice_id"].map(paid_doc).fillna(0.0))
        sub = round(float((open_doc * e_inv["fx_rate"]).sum()), 2)

        e_ap = ap[ap["entity"] == entity]
        glbal = round(float(e_ap["credit_usd"].sum() - e_ap["debit_usd"].sum()), 2)
        var = round(glbal - sub, 2)

        # (a) invoices in the sub-ledger with no accrual journal at all
        missing = e_inv[~e_inv["invoice_id"].isin(posted_ids)]
        missing_gl = -round(float(missing["invoice_amount_usd"].sum()), 2)

        # (b) accruals posted at a rate other than the published period rate
        booked = e_inv[e_inv["invoice_id"].isin(posted_ids)]
        fx_posting = round(float(
            (booked["invoice_id"].map(accrual_credit) - booked["invoice_amount_usd"]).sum()), 2)

        # (c) manual entries straight to the AP control account
        tops = e_ap[e_ap["source_module"] != "AP"]
        topside = round(float(tops["credit_usd"].sum() - tops["debit_usd"].sum()), 2)

        # (d) FX movement between accrual and settlement of the same payable
        e_applied = applied[applied["payment_id"].map(pay_entity) == entity]
        rate_map = inv.set_index("invoice_id")["fx_rate"]
        settle_at_accrual_rate = float(
            (e_applied["payment_amount"] * e_applied["invoice_id"].map(rate_map)).sum())
        relieved = float(e_applied["payment_id"].map(relief_debit).fillna(0.0).sum())
        fx_settlement = round(settle_at_accrual_rate - relieved, 2)

        explained = round(missing_gl + fx_posting + topside + fx_settlement, 2)
        residual = round(var - explained, 2)
        rows.append({
            "entity": entity,
            "subledger_ap_open_usd": sub,
            "gl_ap_control_usd": glbal,
            "variance_usd": var,
            "explained_missing_gl_postings_usd": missing_gl,
            "explained_fx_posting_errors_usd": fx_posting,
            "explained_topside_entries_usd": topside,
            "explained_fx_on_settlement_usd": fx_settlement,
            "explained_total_usd": explained,
            "unexplained_residual_usd": residual,
            "within_threshold": bool(abs(var) <= TOL_TIEOUT),
        })

    tiedf = pd.DataFrame(rows)
    tie_exceptions = []
    for r in tiedf.itertuples():
        if abs(r.variance_usd) <= TOL_TIEOUT:
            continue
        tie_exceptions.append(ex(
            "C-11", "GL_SUBLEDGER_VARIANCE", entity=r.entity, source_date=AS_OF,
            amount=abs(r.variance_usd),
            rule=f"|GL AP control - AP sub-ledger open| > USD {TOL_TIEOUT:,.0f} at close",
            evidence=(f"{r.entity}: GL AP control USD {r.gl_ap_control_usd:,.2f} vs sub-ledger "
                      f"open USD {r.subledger_ap_open_usd:,.2f}, variance USD "
                      f"{r.variance_usd:,.2f}. Decomposed: missing GL postings "
                      f"{r.explained_missing_gl_postings_usd:,.2f}, FX posting errors "
                      f"{r.explained_fx_posting_errors_usd:,.2f}, topside entries "
                      f"{r.explained_topside_entries_usd:,.2f}, FX on settlement "
                      f"{r.explained_fx_on_settlement_usd:,.2f}, unexplained "
                      f"{r.unexplained_residual_usd:,.2f}"),
            key_type="entity", key=r.entity))
    return tiedf, tie_exceptions


# --------------------------------------------------------- workflow enrichment

def severity(row):
    amt = row["exception_amount_usd"]
    t = row["exception_type"]
    base = ("Critical" if amt >= SEV_CRITICAL else
            "High" if amt >= SEV_HIGH else
            "Medium" if amt >= SEV_MEDIUM else "Low")
    order = ["Low", "Medium", "High", "Critical"]
    floor = {"SOD_VIOLATION": "High", "TOPSIDE_AP_ENTRY": "High",
             "DUPLICATE_PAYMENT": "High", "GL_SUBLEDGER_VARIANCE": "Critical",
             "DUPLICATE_VENDOR_MASTER": "Medium"}.get(t)
    if floor and order.index(floor) > order.index(base):
        return floor
    return base


def enrich(exceptions):
    """Assign IDs, severity, owning queue, and a simulated workflow state.
    Workflow state is a deterministic function of the exception ID hash and the
    age of the item - older exceptions are mostly closed, recent ones are not."""
    df = pd.DataFrame(exceptions).sort_values(
        ["source_date", "control_id", "match_key"]).reset_index(drop=True)
    df["exception_id"] = [f"EXC-{i:05d}" for i in range(1, len(df) + 1)]
    df["severity"] = df.apply(severity, axis=1)
    df["assigned_queue"] = df["control_id"].map(QUEUE)
    df["period"] = df["source_date"].dt.strftime("%Y-%m")
    df["detected_date"] = df["source_date"] + pd.to_timedelta(2, unit="D")
    df.loc[df["detected_date"] > pd.Timestamp(AS_OF), "detected_date"] = pd.Timestamp(AS_OF)
    df["age_days"] = (pd.Timestamp(AS_OF) - df["detected_date"]).dt.days
    df["sla_days"] = df["severity"].map(SLA_DAYS)

    statuses, res_dates, days_to_res, recovered = [], [], [], []
    for r in df.itertuples():
        rng = random.Random(int(hashlib.md5(r.exception_id.encode()).hexdigest()[:8], 16))
        age = r.age_days
        p_res = (0.90 if age > 150 else 0.78 if age > 90 else 0.62 if age > 60
                 else 0.42 if age > 30 else 0.12)
        if r.severity in ("Critical", "High"):
            p_res = min(0.95, p_res + 0.07)
        roll = rng.random()
        if roll < p_res:
            status = "Resolved"
            d = min(int(rng.triangular(1, max(2, r.sla_days), max(3, r.sla_days * 3))), age)
            d = max(1, d)
            res_dates.append(r.detected_date + timedelta(days=int(d)))
            days_to_res.append(int(d))
            rec = 0.0
            if r.exception_type in ("DUPLICATE_PAYMENT", "PAYMENT_AMOUNT_MISMATCH",
                                    "DUPLICATE_INVOICE"):
                rec = round(r.exception_amount_usd * rng.uniform(0.62, 1.0), 2)
            recovered.append(rec)
        elif roll < p_res + (1 - p_res) * 0.45:
            status = "In Review"
            res_dates.append(pd.NaT); days_to_res.append(None); recovered.append(0.0)
        else:
            status = "Open"
            res_dates.append(pd.NaT); days_to_res.append(None); recovered.append(0.0)
        statuses.append(status)

    df["exception_status"] = statuses
    df["resolution_date"] = res_dates
    df["days_to_resolve"] = days_to_res
    df["recovered_amount_usd"] = recovered
    df["sla_breached"] = ((df["exception_status"] != "Resolved") &
                          (df["age_days"] > df["sla_days"])) | \
                         ((df["exception_status"] == "Resolved") &
                          (df["days_to_resolve"].fillna(0) > df["sla_days"]))
    df["resolution_period"] = pd.to_datetime(df["resolution_date"]).dt.strftime("%Y-%m")
    df["requires_human_review"] = (df["severity"].isin(["Critical", "High"])) | \
                                  (df["exception_amount_usd"] >= SEV_HIGH)
    cols = ["exception_id", "control_id", "control_name", "exception_type", "severity",
            "entity", "period", "vendor_id", "invoice_id", "payment_id", "je_id",
            "bank_reference", "exception_amount_usd", "recovered_amount_usd",
            "source_date", "detected_date", "age_days", "exception_status",
            "resolution_date", "resolution_period", "days_to_resolve", "sla_days",
            "sla_breached",
            "assigned_queue", "root_cause_category", "requires_human_review",
            "detection_rule", "evidence", "match_key_type", "match_key"]
    return df[cols]


# ------------------------------------------------------------------- aging

def aging(inv, pay, vendors):
    paid = pay[pay["invoice_id"] != ""].groupby("invoice_id")["payment_amount_usd"].sum()
    vmap = vendors.set_index("vendor_id")["vendor_name"].to_dict()
    rows = []
    for r in inv.itertuples():
        bal = round(r.invoice_amount_usd - float(paid.get(r.invoice_id, 0.0)), 2)
        if r.invoice_status == "Paid" or bal <= 0.01:
            continue
        days = (AS_OF - r.due_date.date()).days
        bucket = ("Current" if days <= 0 else "1-30" if days <= 30 else
                  "31-60" if days <= 60 else "61-90" if days <= 90 else
                  "91-180" if days <= 180 else "180+")
        rows.append({"invoice_id": r.invoice_id, "vendor_id": r.vendor_id,
                     "vendor_name": vmap.get(r.vendor_id, r.vendor_id),
                     "entity": r.entity, "invoice_status": r.invoice_status,
                     "currency": r.currency, "due_date": r.due_date.date().isoformat(),
                     "days_past_due": days, "aging_bucket": bucket,
                     "open_balance_usd": bal})
    return pd.DataFrame(rows)


# -------------------------------------------------------- detection scorecard

SEEDED_CONTROLS = ["C-01", "C-02", "C-03", "C-04", "C-05", "C-06", "C-07",
                   "C-08", "C-09", "C-10", "C-12"]


def scorecard(exdf):
    """Measure the engine against the seeded defect log.

    A detection whose key is not in the seeded set for that control is not
    automatically wrong. Some are *corroborating* detections: a duplicate
    invoice that was then paid shows up again, correctly, as a duplicate
    payment. Those are separated from genuine false positives by tracing the
    detection's own keys back to the seeded log.
    """
    seeded = pd.read_csv(os.path.join(DATA, "seeded_defects.csv"))
    seeded_universe = set(seeded["key"].astype(str)) | \
        set(seeded["related_key"].fillna("").astype(str))
    seeded_universe.discard("")

    rows = []
    for ctrl in SEEDED_CONTROLS:
        for dtype in sorted(set(seeded.loc[seeded["control_id"] == ctrl, "defect_type"])):
            s_keys = set(seeded.loc[(seeded["control_id"] == ctrl) &
                                    (seeded["defect_type"] == dtype), "key"])
            det = exdf[(exdf["control_id"] == ctrl) & (exdf["exception_type"] == dtype)]
            d_keys = set(det["match_key"])
            tp = len(s_keys & d_keys)
            fn = len(s_keys - d_keys)
            extra = d_keys - s_keys
            # pair-level recall: for paired defects (a duplicate and its
            # original) flagging either record identifies the pair
            pair_rows = seeded.loc[(seeded["control_id"] == ctrl) &
                                   (seeded["defect_type"] == dtype)]
            pair_hits = sum(
                1 for _, sr in pair_rows.iterrows()
                if str(sr["key"]) in d_keys or
                (str(sr.get("related_key") or "") and str(sr["related_key"]) in d_keys))
            corroborated = 0
            for _, r in det[det["match_key"].isin(extra)].iterrows():
                linked = {str(r["invoice_id"]), str(r["payment_id"]), str(r["je_id"]),
                          str(r["bank_reference"]), str(r["vendor_id"])}
                if linked & seeded_universe:
                    corroborated += 1
            fp = len(extra) - corroborated
            rows.append({
                "control_id": ctrl, "control_name": CONTROL_NAMES[ctrl],
                "exception_type": dtype, "seeded": len(s_keys), "detected": len(d_keys),
                "true_positives": tp, "false_negatives": fn,
                "corroborating_detections": corroborated, "false_positives": fp,
                "recall_pct": round(100 * tp / len(s_keys), 1) if s_keys else None,
                "pair_recall_pct": round(100 * pair_hits / len(pair_rows), 1)
                if len(pair_rows) else None,
                "precision_pct": round(100 * (tp + corroborated) / len(d_keys), 1)
                if d_keys else None,
                "note": "",
            })
    for ctrl, note in [("C-11", "Derived tie-out - no seeded population"),
                       ("C-13", "Operational aging review - no seeded population")]:
        d = exdf[exdf["control_id"] == ctrl]
        rows.append({"control_id": ctrl, "control_name": CONTROL_NAMES[ctrl],
                     "exception_type": d["exception_type"].iloc[0] if len(d) else "",
                     "seeded": None, "detected": len(d), "true_positives": None,
                     "false_negatives": None, "corroborating_detections": None,
                     "false_positives": None, "recall_pct": None, "pair_recall_pct": None,
                     "precision_pct": None, "note": note})
    return pd.DataFrame(rows)


# -------------------------------------------------------------------- outputs

def build_json(d, exdf, agedf, tiedf, scoredf):
    inv, pay, gl, bank = d["invoices"], d["payments"], d["gl_entries"], d["bank_transactions"]
    vend = d["vendors"]
    open_ex = exdf[exdf["exception_status"] != "Resolved"]

    def grp(df, by, value="exception_amount_usd"):
        g = df.groupby(by).agg(count=("exception_id", "count"),
                               value_usd=(value, "sum")).reset_index()
        g["value_usd"] = g["value_usd"].round(2)
        return g.sort_values("value_usd", ascending=False).to_dict("records")

    periods = sorted(exdf["period"].unique())
    raised = exdf.groupby("period")["exception_id"].count().to_dict()
    raised_val = exdf.groupby("period")["exception_amount_usd"].sum().round(2).to_dict()
    cleared = exdf[exdf["exception_status"] == "Resolved"] \
        .groupby("resolution_period")["exception_id"].count().to_dict()
    inflow_outflow = [{"period": p, "raised": int(raised.get(p, 0)),
                       "raised_value_usd": float(raised_val.get(p, 0.0)),
                       "cleared": int(cleared.get(p, 0))} for p in periods]
    backlog, run = [], 0
    for r in inflow_outflow:
        run += r["raised"] - r["cleared"]
        backlog.append({"period": r["period"], "open_backlog": run})

    buckets = ["Current", "1-30", "31-60", "61-90", "91-180", "180+"]
    ag = agedf.groupby("aging_bucket").agg(invoices=("invoice_id", "count"),
                                           open_balance_usd=("open_balance_usd", "sum")) \
        .reindex(buckets).fillna(0).reset_index()
    ag["open_balance_usd"] = ag["open_balance_usd"].round(2)
    ag["invoices"] = ag["invoices"].astype(int)

    res = exdf[exdf["exception_status"] == "Resolved"]
    mttr_by_ctrl = (res.groupby("control_id")["days_to_resolve"].mean().round(1)
                    .reset_index().rename(columns={"days_to_resolve": "avg_days_to_resolve"}))
    mttr_by_ctrl["control_name"] = mttr_by_ctrl["control_id"].map(CONTROL_NAMES)

    top_vendors = (exdf[exdf["vendor_id"] != ""]
                   .groupby("vendor_id")
                   .agg(exceptions=("exception_id", "count"),
                        value_usd=("exception_amount_usd", "sum")).reset_index())
    top_vendors["value_usd"] = top_vendors["value_usd"].round(2)
    top_vendors = top_vendors.merge(
        vend[["vendor_id", "vendor_name", "vendor_category", "risk_tier"]],
        on="vendor_id", how="left").sort_values("value_usd", ascending=False).head(12)

    sc = scoredf[scoredf["seeded"].notna()]
    out = {
        "meta": {
            "company": "Northwind Services Group",
            "as_of_date": AS_OF.isoformat(),
            "period_range": "2026-01 to 2026-09",
            "generated_at": datetime.now().replace(microsecond=0).isoformat(),
            "thresholds": {
                "payment_tolerance_abs_usd": TOL_ABS,
                "payment_tolerance_pct": TOL_PCT,
                "journal_balance_tolerance_usd": TOL_JE,
                "fx_tolerance_abs_usd": TOL_FX_ABS,
                "fx_tolerance_pct": TOL_FX_PCT,
                "tieout_threshold_usd": TOL_TIEOUT,
                "aged_item_days": AGED_DAYS,
                "severity_ladder_usd": {"critical": SEV_CRITICAL, "high": SEV_HIGH,
                                        "medium": SEV_MEDIUM},
            },
        },
        "population": {
            "vendors": int(len(vend)),
            "invoices": int(len(inv)),
            "invoice_value_usd": round(float(inv["invoice_amount_usd"].sum()), 2),
            "payments": int(len(pay)),
            "payment_value_usd": round(float(pay["payment_amount_usd"].sum()), 2),
            "gl_lines": int(len(gl)),
            "gl_value_usd": round(float(gl["debit_usd"].sum()), 2),
            "bank_transactions": int(len(bank)),
            "controls_run": len(CONTROL_NAMES),
        },
        "kpis": {
            "total_exceptions": int(len(exdf)),
            "total_exception_value_usd": round(float(exdf["exception_amount_usd"].sum()), 2),
            "open_exceptions": int(len(open_ex)),
            "open_exception_value_usd": round(float(open_ex["exception_amount_usd"].sum()), 2),
            "resolved_exceptions": int((exdf["exception_status"] == "Resolved").sum()),
            "exception_rate_pct": round(100 * len(exdf) / len(inv), 2),
            "critical_open": int((open_ex["severity"] == "Critical").sum()),
            "sla_breached_open": int(open_ex["sla_breached"].sum()),
            "avg_days_to_resolve": round(float(res["days_to_resolve"].mean()), 1),
            "recovered_amount_usd": round(float(exdf["recovered_amount_usd"].sum()), 2),
            "cash_at_risk_usd": round(float(exdf.loc[exdf["exception_type"].isin(
                ["DUPLICATE_PAYMENT", "PAYMENT_AMOUNT_MISMATCH", "DUPLICATE_INVOICE",
                 "UNAPPLIED_PAYMENT"]), "exception_amount_usd"].sum()), 2),
            "unreconciled_ap_variance_usd": round(float(tiedf["variance_usd"].abs().sum()), 2),
            "unexplained_residual_usd": round(float(tiedf["unexplained_residual_usd"].sum()), 2),
            "open_ap_balance_usd": round(float(agedf["open_balance_usd"].sum()), 2),
            "past_due_balance_usd": round(float(
                agedf.loc[agedf["aging_bucket"] != "Current", "open_balance_usd"].sum()), 2),
            "detection_recall_pct": round(
                100 * sc["true_positives"].sum() / sc["seeded"].sum(), 1),
            "detection_pair_recall_pct": round(float(
                (sc["pair_recall_pct"] * sc["seeded"]).sum() / sc["seeded"].sum()), 1),
            "detection_precision_pct": round(
                100 * (sc["true_positives"].sum() + sc["corroborating_detections"].sum())
                / sc["detected"].sum(), 1),
            "detection_false_positives": int(sc["false_positives"].sum()),
            "seeded_defects": int(sc["seeded"].sum()),
        },
        "by_control": grp(exdf, ["control_id", "control_name", "exception_type"]),
        "by_severity": grp(exdf, ["severity"]),
        "by_entity": grp(exdf, ["entity"]),
        "by_status": grp(exdf, ["exception_status"]),
        "by_queue": grp(exdf, ["assigned_queue"]),
        "by_root_cause": grp(exdf, ["root_cause_category"]),
        "open_by_severity": grp(open_ex, ["severity"]),
        "trend": inflow_outflow,
        "backlog": backlog,
        "aging": ag.to_dict("records"),
        "aging_by_entity": agedf.groupby(["entity", "aging_bucket"])["open_balance_usd"]
            .sum().round(2).reset_index().to_dict("records"),
        "tieout": tiedf.to_dict("records"),
        "mttr_by_control": mttr_by_ctrl.to_dict("records"),
        "top_vendors": top_vendors.to_dict("records"),
        "scorecard": scoredf.where(pd.notna(scoredf), None).to_dict("records"),
        "largest_exceptions": (exdf.sort_values("exception_amount_usd", ascending=False)
                               .head(15)[["exception_id", "control_id", "exception_type",
                                          "severity", "entity", "vendor_id",
                                          "exception_amount_usd", "exception_status",
                                          "evidence"]].to_dict("records")),
    }
    return out


def main():
    random.seed(4242)
    os.makedirs(OUT, exist_ok=True)
    d = load()
    inv, pay, gl, bank = d["invoices"], d["payments"], d["gl_entries"], d["bank_transactions"]

    exceptions = []
    exceptions += c01_duplicate_invoices(inv)
    exceptions += c02_duplicate_payments(pay, inv)
    exceptions += c03_unapplied_payments(pay, inv)
    exceptions += c04_missing_gl(inv, gl)
    exceptions += c05_amount_mismatch(pay, inv)
    exceptions += c06_bank_breaks(pay, gl, bank)
    exceptions += c07_unbalanced_journals(gl)
    exceptions += c08_sod(gl)
    exceptions += c09_fx_rate(inv, gl, d["fx_rates"])
    exceptions += c10_topside(gl)
    exceptions += c12_duplicate_vendors(d["vendors"])
    exceptions += c13_aged_items(inv, pay)

    tiedf, tie_ex = c11_tieout(inv, pay, gl)
    exceptions += tie_ex

    exdf = enrich(exceptions)
    agedf = aging(inv, pay, d["vendors"])
    scoredf = scorecard(exdf)

    exdf.to_csv(os.path.join(OUT, "exceptions.csv"), index=False)
    agedf.sort_values("open_balance_usd", ascending=False) \
        .to_csv(os.path.join(OUT, "invoice_aging.csv"), index=False)
    tiedf.to_csv(os.path.join(OUT, "gl_vs_subledger.csv"), index=False)
    scoredf.to_csv(os.path.join(OUT, "detection_scorecard.csv"), index=False)

    payload = build_json(d, exdf, agedf, tiedf, scoredf)
    with open(os.path.join(OUT, "recon_output.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    k = payload["kpis"]
    print(f"Reconciliation run - as of {AS_OF}")
    print(f"  population      {payload['population']['invoices']:,} invoices / "
          f"{payload['population']['payments']:,} payments / "
          f"{payload['population']['gl_lines']:,} GL lines")
    print(f"  exceptions      {k['total_exceptions']:,} "
          f"(USD {k['total_exception_value_usd']:,.0f}), "
          f"{k['open_exceptions']:,} still open")
    print(f"  cash at risk    USD {k['cash_at_risk_usd']:,.0f} "
          f"| recovered USD {k['recovered_amount_usd']:,.0f}")
    print(f"  AP tie-out      variance USD {k['unreconciled_ap_variance_usd']:,.2f}, "
          f"unexplained USD {k['unexplained_residual_usd']:,.2f}")
    print(f"  detection       recall {k['detection_recall_pct']}% / "
          f"precision {k['detection_precision_pct']}% on "
          f"{k['seeded_defects']} seeded defects")
    print("  by control:")
    for r in payload["by_control"]:
        print(f"    {r['control_id']} {r['exception_type']:<26} {r['count']:>5}  "
              f"USD {r['value_usd']:>14,.2f}")
    print("  wrote output/exceptions.csv, invoice_aging.csv, gl_vs_subledger.csv, "
          "detection_scorecard.csv, recon_output.json")


if __name__ == "__main__":
    main()
