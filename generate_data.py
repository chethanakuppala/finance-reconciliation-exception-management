#!/usr/bin/env python3
"""
generate_data.py - synthetic finance source systems for the reconciliation pipeline.

Simulates four source systems for a fictional company (Northwind Services Group,
FY2026 periods 2026-01 .. 2026-09, consolidated USD ledger):

  AP sub-ledger   -> data/vendors.csv, data/invoices.csv
  Disbursements   -> data/payments.csv
  General ledger  -> data/gl_entries.csv
  Bank statements -> data/bank_transactions.csv
  FX rate table   -> data/fx_rates.csv

Every clean invoice flows: invoice -> AP accrual JE -> payment -> payment JE ->
bank clearing. A controlled number of *defects* are then injected on top of that
clean population (duplicate invoices, duplicate payments, unapplied cash,
interface drops, short/over payments, unbalanced manual JEs, segregation-of-duty
breaches, wrong FX rates, topside entries to the AP control account, duplicate
vendor master records).

Each injected defect is logged to data/seeded_defects.csv. That file is the
ground truth used by reconciliation.py to measure detection recall per control -
it is deliberately NOT read by the matching logic itself.

Deterministic: fixed seed, same output every run.
"""

import csv
import json
import os
import random
from datetime import date, timedelta

SEED = 20260930
random.seed(SEED)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

COMPANY = "Northwind Services Group"
PERIOD_START = date(2026, 1, 1)
PERIOD_END = date(2026, 9, 30)
AS_OF = date(2026, 9, 30)

N_VENDORS = 58
N_INVOICES = 1800

# ---------------------------------------------------------------- master data

ENTITIES = [
    ("US01", "Northwind Services US", "USD"),
    ("CA01", "Northwind Services Canada", "USD"),
    ("UK01", "Northwind Services UK", "USD"),
]

COST_CENTERS = [
    ("CC-100", "Finance & Accounting"),
    ("CC-200", "Information Technology"),
    ("CC-300", "Operations"),
    ("CC-400", "Sales & Marketing"),
    ("CC-500", "Facilities"),
    ("CC-600", "Human Resources"),
]

GL_ACCOUNTS = [
    ("10100", "Cash - Operating Account", "Asset", "Cash"),
    ("13100", "Input Tax Receivable", "Asset", "Tax"),
    ("20100", "Accounts Payable - Trade", "Liability", "AP Control"),
    ("20150", "Accrued Liabilities", "Liability", "Accrual"),
    ("60100", "Cost of Services Delivered", "Expense", "Direct Cost"),
    ("61000", "IT & Software", "Expense", "Operating Expense"),
    ("62000", "Professional Fees", "Expense", "Operating Expense"),
    ("63000", "Facilities & Utilities", "Expense", "Operating Expense"),
    ("64000", "Marketing & Advertising", "Expense", "Operating Expense"),
    ("65000", "Travel & Entertainment", "Expense", "Operating Expense"),
    ("66000", "Logistics & Freight", "Expense", "Operating Expense"),
    ("70000", "Foreign Exchange Gain / Loss", "Expense", "Other"),
    ("99999", "Suspense / Clearing", "Asset", "Clearing"),
]

AP_CONTROL = "20100"
CASH_ACCOUNT = "10100"
TAX_ACCOUNT = "13100"
FX_ACCOUNT = "70000"
SUSPENSE = "99999"

# vendor category -> (expense account, default cost center pool)
CATEGORIES = {
    "IT & Software":        ("61000", ["CC-200", "CC-100"]),
    "Professional Services": ("62000", ["CC-100", "CC-600", "CC-300"]),
    "Facilities":           ("63000", ["CC-500", "CC-300"]),
    "Marketing Agency":     ("64000", ["CC-400"]),
    "Travel":               ("65000", ["CC-400", "CC-300", "CC-600"]),
    "Logistics":            ("66000", ["CC-300"]),
    "Subcontractor":        ("60100", ["CC-300", "CC-200"]),
}

COUNTRY_CCY = {
    "United States": "USD",
    "Canada": "CAD",
    "United Kingdom": "GBP",
    "Germany": "EUR",
    "Netherlands": "EUR",
    "India": "INR",
    "Singapore": "SGD",
}

VENDOR_STEMS = [
    "Apex", "Blue Ridge", "Cedarline", "Meridian", "Northgate", "Orion",
    "Pinnacle", "Quarry Hill", "Redstone", "Saltgrass", "Tidewater", "Umbra",
    "Vantage", "Westbrook", "Yarrow", "Zenith", "Brightpath", "Clearwater",
    "Dunmore", "Eastvale", "Fairmount", "Granite Bay", "Harborview",
    "Ironwood", "Juniper", "Kestrel", "Larkfield", "Maplecrest", "Oakhurst",
    "Palisade", "Riverbend", "Stonebridge", "Thornhill", "Vireo", "Whitlock",
    "Ashgrove", "Belmont", "Coppervale", "Drayton", "Elmwood", "Foxglove",
    "Glenmoor", "Hollis", "Inverness", "Kingsley", "Lochmere", "Monterey",
    "Norwood", "Overton", "Parkhurst", "Rosemont", "Sandalwood", "Tolliver",
    "Underhill", "Vermillion", "Waterford", "Yardley", "Zephyr",
]
VENDOR_SUFFIX = {
    "IT & Software": ["Systems Inc", "Software Ltd", "Technologies LLC", "Digital Inc"],
    "Professional Services": ["Advisory LLP", "Consulting Group", "Partners LLP"],
    "Facilities": ["Facilities Services", "Property Group", "Utilities Co"],
    "Marketing Agency": ["Creative Agency", "Media Group", "Brand Studio"],
    "Travel": ["Travel Management", "Corporate Travel", "Mobility Services"],
    "Logistics": ["Logistics Inc", "Freight Lines", "Supply Chain Ltd"],
    "Subcontractor": ["Contracting LLC", "Field Services", "Technical Services"],
}

TERMS = ["NET15", "NET30", "NET30", "NET30", "NET45", "NET60"]
TERM_DAYS = {"NET15": 15, "NET30": 30, "NET45": 45, "NET60": 60}

AP_CLERKS = ["jmorales", "tnakamura", "rpatel", "sokafor", "dlindqvist", "cbarros"]
AP_APPROVERS = ["kwhitfield", "mduarte", "ahassan", "lferraro"]
GL_ACCOUNTANTS = ["pvargas", "ebeaumont", "nsalvatore", "hkowalski"]

PAYMENT_METHODS = (["ACH"] * 12) + (["Wire"] * 5) + (["Check"] * 3)
BANK_ACCOUNTS = {"US01": "****4417", "CA01": "****9102", "UK01": "****6630"}

# ------------------------------------------------------------------- helpers

def biz_day(d):
    """Roll a date forward to the next weekday."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def rand_date(start, end):
    return start + timedelta(days=random.randint(0, (end - start).days))


def money(x):
    return round(x + 0.0, 2)


def period_of(d):
    return f"{d.year}-{d.month:02d}"


# ----------------------------------------------------------------- fx rates

def build_fx_rates():
    """Monthly average rate per currency, USD per 1 unit of foreign currency."""
    base = {"USD": 1.0, "CAD": 0.735, "GBP": 1.272, "EUR": 1.086, "INR": 0.0119, "SGD": 0.745}
    rows, lookup = [], {}
    d = date(PERIOD_START.year, PERIOD_START.month, 1)
    while d <= PERIOD_END:
        per = period_of(d)
        for ccy, b in base.items():
            if ccy == "USD":
                rate = 1.0
            else:
                drift = random.uniform(-0.028, 0.028)
                rate = round(b * (1 + drift), 6)
            rows.append({"period": per, "from_currency": ccy, "to_currency": "USD",
                         "rate_type": "MONTHLY_AVG", "fx_rate": rate,
                         "effective_date": d.isoformat(), "rate_source": "Treasury Rate Feed"})
            lookup[(per, ccy)] = rate
        d = date(d.year + (d.month // 12), (d.month % 12) + 1, 1)
    return rows, lookup


FX_ROWS, FX = build_fx_rates()


def fx_rate(d, ccy):
    return FX.get((period_of(d), ccy), 1.0)


# -------------------------------------------------------------------- vendors

def build_vendors():
    vendors = []
    stems = VENDOR_STEMS[:]
    random.shuffle(stems)
    cats = list(CATEGORIES.keys())
    countries = (["United States"] * 26 + ["Canada"] * 4 + ["United Kingdom"] * 3 +
                 ["Germany"] * 2 + ["Netherlands"] + ["India"] * 2 + ["Singapore"])
    for i in range(N_VENDORS):
        cat = cats[i % len(cats)]
        country = random.choice(countries)
        stem = stems[i % len(stems)]
        vendors.append({
            "vendor_id": f"V{1000 + i}",
            "vendor_name": f"{stem} {random.choice(VENDOR_SUFFIX[cat])}",
            "vendor_category": cat,
            "country": country,
            "currency": COUNTRY_CCY[country],
            "payment_terms": random.choice(TERMS),
            "bank_account_last4": f"{random.randint(1000, 9999)}",
            "vendor_status": "Active" if random.random() > 0.05 else "Inactive",
            "risk_tier": random.choices(["Low", "Medium", "High"], weights=[6, 3, 1])[0],
            "onboarded_date": rand_date(date(2019, 1, 1), date(2025, 12, 31)).isoformat(),
            "tax_id": f"{random.randint(10, 99)}-{random.randint(1000000, 9999999)}",
        })
    return vendors


# ------------------------------------------------------- ledger / doc builders

class Ledger:
    """Collects GL journal lines and hands out document numbers."""

    def __init__(self):
        self.rows = []
        self._je = 0

    def next_je(self, d):
        self._je += 1
        return f"JE-{d.year}-{self._je:06d}"

    def post(self, je_id, d, entity, lines, source_module, source_doc_id,
             description, created_by, approved_by, je_type="System"):
        for n, (acct, cc, dr, cr) in enumerate(lines, start=1):
            self.rows.append({
                "je_id": je_id,
                "je_line": n,
                "posting_date": d.isoformat(),
                "period": period_of(d),
                "entity": entity,
                "gl_account": acct,
                "cost_center": cc,
                "debit_usd": money(dr),
                "credit_usd": money(cr),
                "source_module": source_module,
                "source_doc_id": source_doc_id,
                "je_type": je_type,
                "description": description,
                "created_by": created_by,
                "approved_by": approved_by,
            })


LEDGER = Ledger()
DEFECTS = []


def log_defect(control_id, defect_type, key_type, key, related_key, amount, entity, desc, d):
    DEFECTS.append({
        "defect_id": f"SD-{len(DEFECTS) + 1:04d}",
        "control_id": control_id,
        "defect_type": defect_type,
        "key_type": key_type,
        "key": key,
        "related_key": related_key or "",
        "amount_usd": money(amount),
        "entity": entity,
        "injected_date": d.isoformat(),
        "description": desc,
    })


# ------------------------------------------------------------------- invoices

def build_invoices(vendors):
    invoices = []
    vmap = {v["vendor_id"]: v for v in vendors}
    active = [v for v in vendors if v["vendor_status"] == "Active"]
    for i in range(N_INVOICES):
        v = random.choice(active)
        entity_code, _, _ = random.choice(ENTITIES)
        acct, cc_pool = CATEGORIES[v["vendor_category"]]
        inv_date = biz_day(rand_date(PERIOD_START, PERIOD_END - timedelta(days=1)))
        terms = v["payment_terms"]
        # spread invoice sizes across four tiers so materiality analysis has range
        tier = random.choices(["small", "mid", "large", "jumbo"], weights=[52, 30, 14, 4])[0]
        net = {
            "small": random.uniform(220, 4800),
            "mid": random.uniform(4800, 24000),
            "large": random.uniform(24000, 95000),
            "jumbo": random.uniform(95000, 310000),
        }[tier]
        net = money(net)
        tax_rate = 0.0 if random.random() < 0.18 else random.choice([0.05, 0.0825, 0.10, 0.20])
        tax = money(net * tax_rate)
        gross = money(net + tax)
        rate = fx_rate(inv_date, v["currency"])
        invoices.append({
            "invoice_id": f"INV-{100000 + i}",
            "invoice_number": f"{v['vendor_id'][1:]}-{random.randint(10000, 99999)}",
            "vendor_id": v["vendor_id"],
            "entity": entity_code,
            "po_number": f"PO-{random.randint(200000, 299999)}" if random.random() > 0.12 else "",
            "invoice_date": inv_date.isoformat(),
            # an invoice received after the close date would not be in this
            # reconciliation population at all, so cap it at the close
            "received_date": min(biz_day(inv_date + timedelta(days=random.randint(0, 5))),
                                 AS_OF).isoformat(),
            "due_date": biz_day(inv_date + timedelta(days=TERM_DAYS[terms])).isoformat(),
            "payment_terms": terms,
            "currency": v["currency"],
            "net_amount": net,
            "tax_amount": tax,
            "invoice_amount": gross,
            "fx_rate": rate,
            "invoice_amount_usd": money(gross * rate),
            "gl_account": acct,
            "cost_center": random.choice(cc_pool),
            "invoice_status": "",            # set during payment build
            "source_system": "AP_SUBLEDGER",
            "entered_by": random.choice(AP_CLERKS),
            "approved_by": random.choice(AP_APPROVERS),
        })
    invoices.sort(key=lambda r: r["invoice_date"])
    return invoices, vmap


def post_accrual(inv, created_by=None, approved_by=None, rate_override=None):
    """AP accrual JE: Dr expense + Dr input tax, Cr AP control."""
    d = date.fromisoformat(inv["received_date"])
    rate = rate_override if rate_override is not None else inv["fx_rate"]
    net_usd = money(inv["net_amount"] * rate)
    tax_usd = money(inv["tax_amount"] * rate)
    gross_usd = money(net_usd + tax_usd)
    je = LEDGER.next_je(d)
    lines = [(inv["gl_account"], inv["cost_center"], net_usd, 0.0)]
    if tax_usd > 0:
        lines.append((TAX_ACCOUNT, inv["cost_center"], tax_usd, 0.0))
    lines.append((AP_CONTROL, inv["cost_center"], 0.0, gross_usd))
    LEDGER.post(je, d, inv["entity"], lines, "AP", inv["invoice_id"],
                f"AP accrual {inv['invoice_number']} / {inv['vendor_id']}",
                created_by or inv["entered_by"], approved_by or inv["approved_by"])
    return je, gross_usd


# ------------------------------------------------------------------- payments

class Payments:
    def __init__(self):
        self.rows = []
        self._n = 0

    def add(self, inv, pay_date, amount_doc, rate, method=None, status="Cleared",
            applied_invoice=None, run_id=None):
        self._n += 1
        pid = f"PAY-{500000 + self._n}"
        method = method or random.choice(PAYMENT_METHODS)
        row = {
            "payment_id": pid,
            "payment_reference": f"{method[:3].upper()}-{random.randint(7000000, 7999999)}",
            "vendor_id": inv["vendor_id"],
            "invoice_id": applied_invoice if applied_invoice is not None else inv["invoice_id"],
            "entity": inv["entity"],
            "payment_date": pay_date.isoformat(),
            "payment_method": method,
            "currency": inv["currency"],
            "payment_amount": money(amount_doc),
            "fx_rate": rate,
            "payment_amount_usd": money(amount_doc * rate),
            "bank_account": BANK_ACCOUNTS[inv["entity"]],
            "payment_run_id": run_id or f"RUN-{pay_date.strftime('%Y%m%d')}",
            "payment_status": status,
            "created_by": random.choice(AP_CLERKS),
            "approved_by": random.choice(AP_APPROVERS),
        }
        self.rows.append(row)
        return row


PAY = Payments()
BANK = []
_bank_n = [0]


def add_bank_txn(entity, value_date, amount_usd, direction, reference, description,
                 txn_type="Vendor Payment"):
    _bank_n[0] += 1
    BANK.append({
        "bank_txn_id": f"BT-{900000 + _bank_n[0]}",
        "bank_account": BANK_ACCOUNTS[entity],
        "entity": entity,
        "value_date": value_date.isoformat(),
        "amount_usd": money(amount_usd),
        "direction": direction,
        "bank_reference": reference,
        "transaction_type": txn_type,
        "description": description,
        "statement_id": f"STMT-{value_date.strftime('%Y%m')}-{BANK_ACCOUNTS[entity][-4:]}",
    })


def post_payment_je(pay, inv, relieve_usd=None):
    """Payment JE: Dr AP control, Cr cash, FX difference to 70000."""
    d = date.fromisoformat(pay["payment_date"])
    cash_usd = pay["payment_amount_usd"]
    ap_relief = relieve_usd if relieve_usd is not None else cash_usd
    je = LEDGER.next_je(d)
    lines = [(AP_CONTROL, inv["cost_center"], ap_relief, 0.0),
             (CASH_ACCOUNT, inv["cost_center"], 0.0, cash_usd)]
    diff = money(cash_usd - ap_relief)
    if abs(diff) >= 0.01:
        if diff > 0:
            lines.append((FX_ACCOUNT, inv["cost_center"], diff, 0.0))
        else:
            lines.append((FX_ACCOUNT, inv["cost_center"], 0.0, -diff))
    LEDGER.post(je, d, inv["entity"], lines, "AP", pay["payment_id"],
                f"Vendor payment {pay['payment_reference']} / {inv['vendor_id']}",
                pay["created_by"], pay["approved_by"])
    return je


def settle(inv, accrual_usd, clean_bank=True, pay_date=None, amount_doc=None):
    """Create a payment for an invoice, post its JE, and clear it at the bank."""
    due = date.fromisoformat(inv["due_date"])
    recv = date.fromisoformat(inv["received_date"])
    if pay_date is None:
        pay_date = biz_day(due + timedelta(days=random.choice(
            [-6, -3, -1, 0, 1, 2, 4, 7, 11, 18])))
    # a payment can never post before the invoice arrived or after the close date
    pay_date = max(recv, min(pay_date, AS_OF))
    pay_date = biz_day(pay_date) if biz_day(pay_date) <= AS_OF else pay_date
    amount_doc = inv["invoice_amount"] if amount_doc is None else amount_doc
    rate = fx_rate(pay_date, inv["currency"])
    pay = PAY.add(inv, pay_date, amount_doc, rate)
    relief = accrual_usd if abs(amount_doc - inv["invoice_amount"]) < 0.01 else None
    post_payment_je(pay, inv, relieve_usd=relief)
    if clean_bank:
        vd = min(biz_day(pay_date + timedelta(days=random.randint(0, 3))), AS_OF)
        add_bank_txn(inv["entity"], vd, pay["payment_amount_usd"], "Debit",
                     pay["payment_reference"],
                     f"ACH/WIRE vendor disbursement {inv['vendor_id']}")
    return pay


# --------------------------------------------------------------- clean build

def build_clean(invoices, vmap):
    """Post accruals for every invoice and settle the paid population."""
    accruals = {}
    for inv in invoices:
        je, gross_usd = post_accrual(inv)
        accruals[inv["invoice_id"]] = gross_usd

        # Settlement likelihood is a function of how long the invoice has been
        # due as of the close date: old items are almost all paid, so the
        # residual aging tail stays small and believable.
        due = date.fromisoformat(inv["due_date"])
        overdue_days = (AS_OF - due).days
        if overdue_days > 120:
            p_paid = 0.975
        elif overdue_days > 60:
            p_paid = 0.945
        elif overdue_days > 20:
            p_paid = 0.88
        elif overdue_days >= 0:
            p_paid = 0.66
        else:
            p_paid = 0.34                     # not yet due at close
        roll = random.random()
        if roll < p_paid:
            inv["invoice_status"] = "Paid"
            settle(inv, gross_usd)
        else:
            tail = random.random()
            if tail < 0.16 and inv["currency"] == "USD":
                inv["invoice_status"] = "Partially Paid"
                part = money(inv["invoice_amount"] * random.uniform(0.35, 0.7))
                settle(inv, gross_usd, amount_doc=part)
            elif tail < 0.42:
                inv["invoice_status"] = "Disputed"
            else:
                inv["invoice_status"] = "Open"
    return accruals


# ------------------------------------------------------------ defect injection

def inject_duplicate_invoices(invoices, accruals, n=16):
    """C-01: same vendor + same amount re-keyed with a cosmetically different
    invoice number (leading zeros, punctuation, trailing space, case)."""
    pool = [i for i in invoices if i["invoice_status"] == "Paid"][:]
    random.shuffle(pool)
    out = []
    for k, src in enumerate(pool[:n]):
        dup = dict(src)
        orig_num = src["invoice_number"]
        variant = [
            orig_num.replace("-", ""),
            f"0{orig_num}",
            f" {orig_num} ",
            orig_num.lower(),
            f"{orig_num}-A",
        ][k % 5]
        d = biz_day(date.fromisoformat(src["invoice_date"]) + timedelta(days=random.randint(1, 6)))
        dup.update({
            "invoice_id": f"INV-9{k:04d}",
            "invoice_number": variant,
            "invoice_date": d.isoformat(),
            "received_date": min(biz_day(d + timedelta(days=1)), AS_OF).isoformat(),
            "due_date": biz_day(d + timedelta(days=TERM_DAYS[src["payment_terms"]])).isoformat(),
            "invoice_status": "Open" if k % 3 == 0 else "Paid",
            "entered_by": random.choice(AP_CLERKS),
            "source_system": "AP_SUBLEDGER",
        })
        _, gross_usd = post_accrual(dup)
        accruals[dup["invoice_id"]] = gross_usd
        if dup["invoice_status"] == "Paid":
            settle(dup, gross_usd)
        out.append(dup)
        log_defect("C-01", "DUPLICATE_INVOICE", "invoice_id", dup["invoice_id"],
                   src["invoice_id"], gross_usd, dup["entity"],
                   f"Re-keyed duplicate of {src['invoice_id']} ({orig_num} -> {variant})", d)
    return out


def inject_duplicate_payments(invoices, accruals, n=12):
    """C-02: the same invoice paid twice (second run picked up a stale file)."""
    pool = [i for i in invoices if i["invoice_status"] == "Paid"][:]
    random.shuffle(pool)
    for src in pool[:n]:
        first = next((p for p in PAY.rows if p["invoice_id"] == src["invoice_id"]), None)
        if not first:
            continue
        pd_ = biz_day(date.fromisoformat(first["payment_date"]) + timedelta(days=random.randint(1, 9)))
        if pd_ > AS_OF:
            pd_ = AS_OF
        rate = fx_rate(pd_, src["currency"])
        dup = PAY.add(src, pd_, src["invoice_amount"], rate, method=first["payment_method"])
        post_payment_je(dup, src, relieve_usd=dup["payment_amount_usd"])
        vd = min(biz_day(pd_ + timedelta(days=random.randint(0, 2))), AS_OF)
        add_bank_txn(src["entity"], vd, dup["payment_amount_usd"], "Debit",
                     dup["payment_reference"], f"Duplicate disbursement {src['vendor_id']}")
        log_defect("C-02", "DUPLICATE_PAYMENT", "payment_id", dup["payment_id"],
                   first["payment_id"], dup["payment_amount_usd"], src["entity"],
                   f"Second payment against {src['invoice_id']} already settled by {first['payment_id']}",
                   pd_)


def inject_unapplied_payments(invoices, n=14):
    """C-03: cash out the door with no invoice applied (unapplied / on-account)."""
    pool = [i for i in invoices][:]
    random.shuffle(pool)
    for src in pool[:n]:
        pd_ = biz_day(rand_date(date(2026, 2, 1), AS_OF))
        amt = money(random.uniform(1200, 68000))
        rate = fx_rate(pd_, src["currency"])
        pay = PAY.add(src, pd_, amt, rate, applied_invoice="")
        d = pd_
        je = LEDGER.next_je(d)
        LEDGER.post(je, d, src["entity"],
                    [(SUSPENSE, src["cost_center"], pay["payment_amount_usd"], 0.0),
                     (CASH_ACCOUNT, src["cost_center"], 0.0, pay["payment_amount_usd"])],
                    "AP", pay["payment_id"],
                    f"On-account payment {pay['payment_reference']} - no invoice applied",
                    pay["created_by"], pay["approved_by"])
        vd = min(biz_day(pd_ + timedelta(days=random.randint(0, 3))), AS_OF)
        add_bank_txn(src["entity"], vd, pay["payment_amount_usd"], "Debit",
                     pay["payment_reference"], f"On-account payment {src['vendor_id']}")
        log_defect("C-03", "UNAPPLIED_PAYMENT", "payment_id", pay["payment_id"], "",
                   pay["payment_amount_usd"], src["entity"],
                   "Disbursement posted to suspense with no invoice reference", pd_)


def inject_missing_gl(invoices, accruals, n=13):
    """C-04: AP sub-ledger invoice whose accrual JE never reached the GL
    (nightly interface drop) - sub-ledger says payable, GL does not."""
    pool = [i for i in invoices if i["invoice_status"] in ("Open", "Disputed")][:]
    random.shuffle(pool)
    for src in pool[:n]:
        inv_id = src["invoice_id"]
        before = len(LEDGER.rows)
        LEDGER.rows = [r for r in LEDGER.rows
                       if not (r["source_doc_id"] == inv_id and r["source_module"] == "AP"
                               and r["gl_account"] == AP_CONTROL)]
        # drop the whole accrual JE for this invoice
        je_ids = {r["je_id"] for r in LEDGER.rows if r["source_doc_id"] == inv_id}
        LEDGER.rows = [r for r in LEDGER.rows if r["source_doc_id"] != inv_id]
        if len(LEDGER.rows) == before:
            continue
        log_defect("C-04", "MISSING_GL_POSTING", "invoice_id", inv_id, "",
                   accruals.get(inv_id, src["invoice_amount_usd"]), src["entity"],
                   "AP sub-ledger invoice with no corresponding GL accrual entry",
                   date.fromisoformat(src["received_date"]))


def inject_amount_mismatch(invoices, accruals, n=20):
    """C-05: payment amount differs from the invoice beyond tolerance
    (short pay on a disputed line, or fat-finger overpay)."""
    pool = [i for i in invoices if i["invoice_status"] == "Paid"][:]
    random.shuffle(pool)
    picked = 0
    for src in pool:
        if picked >= n:
            break
        pay = next((p for p in PAY.rows if p["invoice_id"] == src["invoice_id"]), None)
        if not pay:
            continue
        over = picked % 3 == 0
        factor = random.uniform(1.02, 1.18) if over else random.uniform(0.80, 0.975)
        new_doc = money(src["invoice_amount"] * factor)
        delta_usd = money((new_doc - pay["payment_amount"]) * pay["fx_rate"])
        pay["payment_amount"] = new_doc
        pay["payment_amount_usd"] = money(new_doc * pay["fx_rate"])
        for r in LEDGER.rows:
            if r["source_doc_id"] == pay["payment_id"]:
                if r["gl_account"] == CASH_ACCOUNT:
                    r["credit_usd"] = pay["payment_amount_usd"]
                elif r["gl_account"] == AP_CONTROL:
                    r["debit_usd"] = pay["payment_amount_usd"]
                elif r["gl_account"] == FX_ACCOUNT:
                    r["debit_usd"] = 0.0
                    r["credit_usd"] = 0.0
        for b in BANK:
            if b["bank_reference"] == pay["payment_reference"]:
                b["amount_usd"] = pay["payment_amount_usd"]
        log_defect("C-05", "PAYMENT_AMOUNT_MISMATCH", "payment_id", pay["payment_id"],
                   src["invoice_id"], abs(delta_usd), src["entity"],
                   ("Overpayment" if over else "Short payment") +
                   f" of {abs(delta_usd):,.2f} USD vs invoice {src['invoice_id']}",
                   date.fromisoformat(pay["payment_date"]))
        picked += 1


def inject_bank_breaks(n_missing_bank=9, n_bank_only=6):
    """C-06: cash is out of GL but never hit the statement, and statement
    items with nothing in the GL (bank fees, FX charges, a wire reversal)."""
    cash_pays = [p for p in PAY.rows if p["payment_status"] == "Cleared"]
    random.shuffle(cash_pays)
    removed = 0
    for pay in cash_pays:
        if removed >= n_missing_bank:
            break
        hit = [b for b in BANK if b["bank_reference"] == pay["payment_reference"]]
        if not hit:
            continue
        for b in hit:
            BANK.remove(b)
        pay["payment_status"] = "Issued - Not Cleared"
        log_defect("C-06", "GL_CASH_NOT_ON_STATEMENT", "payment_id", pay["payment_id"], "",
                   pay["payment_amount_usd"], pay["entity"],
                   "GL cash disbursement with no matching bank statement line",
                   date.fromisoformat(pay["payment_date"]))
        removed += 1

    kinds = [("Bank Fee", "Monthly account analysis fee", 1),
             ("FX Charge", "Cross-border wire FX margin", 1),
             ("Returned Item", "Returned ACH - vendor account closed", -1),
             ("Bank Fee", "Wire transfer fee", 1),
             ("Interest", "Interest credit on operating balance", -1),
             ("Adjustment", "Unidentified statement adjustment", 1)]
    for k in range(n_bank_only):
        kind, desc, sign = kinds[k % len(kinds)]
        entity = random.choice([e[0] for e in ENTITIES])
        vd = biz_day(rand_date(date(2026, 2, 1), AS_OF))
        amt = money(random.uniform(45, 3800) if kind != "Returned Item"
                    else random.uniform(2000, 42000))
        ref = f"BNK-{random.randint(4000000, 4999999)}"
        add_bank_txn(entity, vd, amt, "Debit" if sign > 0 else "Credit", ref, desc, kind)
        log_defect("C-06", "BANK_ITEM_NOT_IN_GL", "bank_reference", ref, "",
                   amt, entity, f"{kind} on statement with no GL entry: {desc}", vd)


def inject_unbalanced_je(n=7):
    """C-07: manual journal where debits != credits (feed truncation / bad upload)."""
    for k in range(n):
        d = biz_day(rand_date(date(2026, 2, 1), AS_OF))
        entity = random.choice([e[0] for e in ENTITIES])
        cc = random.choice(COST_CENTERS)[0]
        amt = money(random.uniform(4000, 120000))
        skew = money(amt * random.uniform(0.02, 0.22))
        je = LEDGER.next_je(d)
        who = random.choice(GL_ACCOUNTANTS)
        LEDGER.post(je, d, entity,
                    [("20150", cc, 0.0, amt),
                     (random.choice(["60100", "62000", "63000"]), cc, amt - skew, 0.0)],
                    "MANUAL", je, "Month-end accrual upload (partial file)",
                    who, random.choice([a for a in AP_APPROVERS]), je_type="Manual")
        log_defect("C-07", "UNBALANCED_JOURNAL", "je_id", je, "", skew, entity,
                   f"Manual JE out of balance by {skew:,.2f} USD", d)


def inject_sod_violations(n=10):
    """C-08: manual JE where preparer and approver are the same user."""
    for k in range(n):
        d = biz_day(rand_date(date(2026, 2, 1), AS_OF))
        entity = random.choice([e[0] for e in ENTITIES])
        cc = random.choice(COST_CENTERS)[0]
        amt = money(random.uniform(9000, 240000))
        who = random.choice(GL_ACCOUNTANTS)
        je = LEDGER.next_je(d)
        LEDGER.post(je, d, entity,
                    [(random.choice(["60100", "61000", "62000", "64000"]), cc, amt, 0.0),
                     ("20150", cc, 0.0, amt)],
                    "MANUAL", je, "Reclass entry - prepared and approved by same user",
                    who, who, je_type="Manual")
        log_defect("C-08", "SOD_VIOLATION", "je_id", je, "", amt, entity,
                   f"Manual JE prepared and approved by the same user ({who})", d)


def inject_fx_rate_errors(invoices, accruals, n=9):
    """C-09: accrual translated at a rate that is not the published period rate."""
    pool = [i for i in invoices if i["currency"] != "USD"][:]
    random.shuffle(pool)
    done = 0
    for src in pool:
        if done >= n:
            break
        rows = [r for r in LEDGER.rows if r["source_doc_id"] == src["invoice_id"]]
        if not rows:
            continue
        bad_rate = money(src["fx_rate"] * random.choice([0.90, 0.93, 1.07, 1.12]))
        factor = bad_rate / src["fx_rate"]
        for r in rows:
            r["debit_usd"] = money(r["debit_usd"] * factor)
            r["credit_usd"] = money(r["credit_usd"] * factor)
            r["description"] = r["description"] + " [rate override]"
        delta = abs(money(accruals[src["invoice_id"]] * (factor - 1)))
        accruals[src["invoice_id"]] = money(accruals[src["invoice_id"]] * factor)
        log_defect("C-09", "FX_RATE_ERROR", "invoice_id", src["invoice_id"], "",
                   delta, src["entity"],
                   f"{src['currency']} accrual translated at {bad_rate} vs published "
                   f"{src['fx_rate']} - USD impact {delta:,.2f}",
                   date.fromisoformat(src["received_date"]))
        done += 1


def inject_topside_entries(n=5):
    """C-10: manual entry straight to the AP control account with no
    sub-ledger document behind it (topside close adjustment)."""
    for k in range(n):
        d = biz_day(rand_date(date(2026, 3, 1), AS_OF))
        entity = random.choice([e[0] for e in ENTITIES])
        cc = "CC-100"
        amt = money(random.uniform(18000, 420000))
        je = LEDGER.next_je(d)
        who = random.choice(GL_ACCOUNTANTS)
        credit = k % 2 == 0
        lines = ([("60100", cc, amt, 0.0), (AP_CONTROL, cc, 0.0, amt)] if credit
                 else [(AP_CONTROL, cc, amt, 0.0), ("60100", cc, 0.0, amt)])
        LEDGER.post(je, d, entity, lines, "MANUAL", je,
                    "Topside close adjustment to AP control - no sub-ledger support",
                    who, random.choice(AP_APPROVERS), je_type="Manual")
        log_defect("C-10", "TOPSIDE_AP_ENTRY", "je_id", je, "", amt, entity,
                   "Manual entry to AP control account with no sub-ledger document", d)


def inject_duplicate_vendors(vendors, n=4):
    """C-12: duplicate vendor master records (same bank account, near-identical
    name) - the upstream cause of most duplicate-invoice exposure."""
    pool = [v for v in vendors if v["vendor_status"] == "Active"][:]
    random.shuffle(pool)
    added = []
    for k, src in enumerate(pool[:n]):
        nid = f"V2{100 + k}"
        name_variant = [
            src["vendor_name"].replace(" Inc", ", Inc."),
            src["vendor_name"].upper(),
            src["vendor_name"] + " (DBA)",
            src["vendor_name"].replace(" ", "  "),
        ][k % 4]
        dup = dict(src)
        dup.update({
            "vendor_id": nid,
            "vendor_name": name_variant,
            "onboarded_date": date(2026, random.randint(1, 8), random.randint(1, 28)).isoformat(),
        })
        added.append(dup)
        log_defect("C-12", "DUPLICATE_VENDOR_MASTER", "vendor_id", nid, src["vendor_id"],
                   0.0, "ALL",
                   f"Vendor master duplicate of {src['vendor_id']} - shared bank account "
                   f"{src['bank_account_last4']}, name variant '{name_variant}'",
                   date.fromisoformat(dup["onboarded_date"]))
    return added


# ------------------------------------------------------------------- writing

def write_csv(path, rows, fieldnames=None):
    fieldnames = fieldnames or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    vendors = build_vendors()
    invoices, vmap = build_invoices(vendors)
    accruals = build_clean(invoices, vmap)

    dups = inject_duplicate_invoices(invoices, accruals)
    invoices.extend(dups)
    inject_duplicate_payments(invoices, accruals)
    inject_unapplied_payments(invoices)
    inject_amount_mismatch(invoices, accruals)
    inject_missing_gl(invoices, accruals)
    inject_fx_rate_errors(invoices, accruals)
    inject_bank_breaks()
    inject_unbalanced_je()
    inject_sod_violations()
    inject_topside_entries()
    vendors.extend(inject_duplicate_vendors(vendors))

    invoices.sort(key=lambda r: (r["invoice_date"], r["invoice_id"]))
    LEDGER.rows.sort(key=lambda r: (r["posting_date"], r["je_id"], r["je_line"]))
    PAY.rows.sort(key=lambda r: (r["payment_date"], r["payment_id"]))
    BANK.sort(key=lambda r: (r["value_date"], r["bank_txn_id"]))

    counts = {
        "vendors.csv": write_csv(f"{DATA_DIR}/vendors.csv", vendors),
        "invoices.csv": write_csv(f"{DATA_DIR}/invoices.csv", invoices),
        "payments.csv": write_csv(f"{DATA_DIR}/payments.csv", PAY.rows),
        "gl_entries.csv": write_csv(f"{DATA_DIR}/gl_entries.csv", LEDGER.rows),
        "bank_transactions.csv": write_csv(f"{DATA_DIR}/bank_transactions.csv", BANK),
        "fx_rates.csv": write_csv(f"{DATA_DIR}/fx_rates.csv", FX_ROWS),
        "chart_of_accounts.csv": write_csv(
            f"{DATA_DIR}/chart_of_accounts.csv",
            [{"gl_account": a, "account_name": n, "account_class": c, "account_group": g}
             for a, n, c, g in GL_ACCOUNTS]),
        "cost_centers.csv": write_csv(
            f"{DATA_DIR}/cost_centers.csv",
            [{"cost_center": c, "cost_center_name": n} for c, n in COST_CENTERS]),
        "entities.csv": write_csv(
            f"{DATA_DIR}/entities.csv",
            [{"entity": e, "entity_name": n, "ledger_currency": c} for e, n, c in ENTITIES]),
        "seeded_defects.csv": write_csv(f"{DATA_DIR}/seeded_defects.csv", DEFECTS),
    }

    dr = sum(r["debit_usd"] for r in LEDGER.rows)
    cr = sum(r["credit_usd"] for r in LEDGER.rows)
    profile = {
        "company": COMPANY,
        "seed": SEED,
        "period_start": PERIOD_START.isoformat(),
        "period_end": PERIOD_END.isoformat(),
        "as_of_date": AS_OF.isoformat(),
        "row_counts": counts,
        "gl_total_debits_usd": money(dr),
        "gl_total_credits_usd": money(cr),
        "gl_net_out_of_balance_usd": money(dr - cr),
        "invoice_value_usd": money(sum(i["invoice_amount_usd"] for i in invoices)),
        "payment_value_usd": money(sum(p["payment_amount_usd"] for p in PAY.rows)),
        "seeded_defects_by_control": {},
    }
    for d in DEFECTS:
        k = f"{d['control_id']} {d['defect_type']}"
        profile["seeded_defects_by_control"][k] = profile["seeded_defects_by_control"].get(k, 0) + 1
    with open(f"{DATA_DIR}/data_profile.json", "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    print(f"{COMPANY} - synthetic source data written to data/")
    for k, v in counts.items():
        print(f"  {k:<26} {v:>7,} rows")
    print(f"  GL debits {dr:>18,.2f} / credits {cr:,.2f} "
          f"(net {dr - cr:,.2f} - seeded out-of-balance)")
    print(f"  seeded defects: {len(DEFECTS)} across "
          f"{len(set(d['control_id'] for d in DEFECTS))} controls")


if __name__ == "__main__":
    main()
