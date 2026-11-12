#!/usr/bin/env python3
"""
build_powerbi_model.py - shape the reconciliation output into a Power BI star schema.

Writes powerbi/ as a set of conformed dimensions and fact tables, plus
relationships.csv (the model wiring) and measures.dax (the measure library).
Load the folder with Get Data > Folder, or point at the individual CSVs; the
relationships and measures are documented in powerbi/DASHBOARD_SPEC.md.

  dim_date, dim_entity, dim_vendor, dim_gl_account, dim_cost_center,
  dim_control, dim_exception_type, dim_severity, dim_status, dim_aging_bucket
  fact_invoice, fact_payment, fact_gl, fact_bank_transaction,
  fact_exception, fact_exception_analysis, fact_ap_tieout

Date keys are integers (YYYYMMDD) so the model has one conformed date table.
fact_exception carries two of them - source_date_key (when the transaction
happened) and detected_date_key (when the control caught it) - a role-playing
dimension handled with one active relationship and USERELATIONSHIP measures.
"""

import json
import os

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
PBI = os.path.join(ROOT, "powerbi")

AS_OF = pd.Timestamp("2026-09-30")


def datekey(s):
    d = pd.to_datetime(s, errors="coerce")
    return d.dt.strftime("%Y%m%d").astype("Int64")


def build_dim_date(start="2025-12-01", end="2026-12-31"):
    d = pd.DataFrame({"date": pd.date_range(start, end, freq="D")})
    d["date_key"] = d["date"].dt.strftime("%Y%m%d").astype(int)
    d["year"] = d["date"].dt.year
    d["quarter"] = "Q" + d["date"].dt.quarter.astype(str)
    d["month_number"] = d["date"].dt.month
    d["month_name"] = d["date"].dt.strftime("%b")
    d["period"] = d["date"].dt.strftime("%Y-%m")
    d["period_sort"] = d["year"] * 100 + d["month_number"]
    d["day_of_month"] = d["date"].dt.day
    d["day_name"] = d["date"].dt.strftime("%a")
    d["is_weekend"] = d["date"].dt.weekday >= 5
    d["is_month_end"] = d["date"].dt.is_month_end
    d["fiscal_period"] = d["period"]
    d["is_in_scope"] = (d["date"] >= "2026-01-01") & (d["date"] <= AS_OF)
    d["date"] = d["date"].dt.strftime("%Y-%m-%d")
    return d[["date_key", "date", "year", "quarter", "month_number", "month_name",
              "period", "period_sort", "day_of_month", "day_name", "is_weekend",
              "is_month_end", "fiscal_period", "is_in_scope"]]


def main():
    os.makedirs(PBI, exist_ok=True)

    inv = pd.read_csv(f"{DATA}/invoices.csv", dtype={"invoice_number": str,
                                                     "gl_account": str})
    pay = pd.read_csv(f"{DATA}/payments.csv")
    gl = pd.read_csv(f"{DATA}/gl_entries.csv", dtype={"gl_account": str})
    bank = pd.read_csv(f"{DATA}/bank_transactions.csv")
    vend = pd.read_csv(f"{DATA}/vendors.csv", dtype={"bank_account_last4": str})
    coa = pd.read_csv(f"{DATA}/chart_of_accounts.csv", dtype={"gl_account": str})
    cc = pd.read_csv(f"{DATA}/cost_centers.csv")
    ent = pd.read_csv(f"{DATA}/entities.csv")
    exc = pd.read_csv(f"{OUT}/exceptions.csv")
    aging = pd.read_csv(f"{OUT}/invoice_aging.csv")
    tie = pd.read_csv(f"{OUT}/gl_vs_subledger.csv")
    an = pd.read_csv(f"{OUT}/exception_analysis.csv")
    score = pd.read_csv(f"{OUT}/detection_scorecard.csv")

    written = {}

    def write(name, df):
        df.to_csv(os.path.join(PBI, f"{name}.csv"), index=False)
        written[name] = len(df)

    # ------------------------------------------------------------ dimensions
    write("dim_date", build_dim_date())
    write("dim_entity", ent)
    write("dim_gl_account", coa)
    write("dim_cost_center", cc)

    # vendor dimension: drop the sensitive attributes, keep the analytics ones
    v = vend.drop(columns=["bank_account_last4", "tax_id"]).copy()
    spend = inv.groupby("vendor_id").agg(
        invoice_count=("invoice_id", "count"),
        spend_usd=("invoice_amount_usd", "sum")).round(2).reset_index()
    v = v.merge(spend, on="vendor_id", how="left").fillna(
        {"invoice_count": 0, "spend_usd": 0.0})
    v["spend_band"] = pd.cut(v["spend_usd"],
                             [-1, 100_000, 500_000, 1_500_000, float("inf")],
                             labels=["Under 100k", "100k-500k", "500k-1.5m", "Over 1.5m"])
    write("dim_vendor", v)

    ctrl = (exc[["control_id", "control_name", "assigned_queue"]]
            .drop_duplicates("control_id").sort_values("control_id"))
    ctrl["control_family"] = ctrl["control_id"].map({
        "C-01": "Duplicate prevention", "C-02": "Duplicate prevention",
        "C-12": "Duplicate prevention",
        "C-03": "Cash application", "C-06": "Cash and bank",
        "C-04": "Completeness", "C-11": "Completeness",
        "C-05": "Accuracy", "C-09": "Accuracy", "C-07": "Accuracy",
        "C-08": "Authorisation", "C-10": "Authorisation",
        "C-13": "Working capital"})
    ctrl = ctrl.merge(
        score.groupby("control_id").agg(
            seeded_defects=("seeded", "sum"),
            recall_pct=("recall_pct", "mean"),
            precision_pct=("precision_pct", "mean")).round(1).reset_index(),
        on="control_id", how="left")
    write("dim_control", ctrl)

    et = (exc[["exception_type", "control_id", "root_cause_category"]]
          .drop_duplicates("exception_type").sort_values("exception_type"))
    write("dim_exception_type", et)

    write("dim_severity", pd.DataFrame({
        "severity": ["Critical", "High", "Medium", "Low"],
        "severity_sort": [1, 2, 3, 4],
        "sla_days": [3, 5, 10, 20],
        "severity_colour": ["#d03b3b", "#ec835a", "#fab219", "#898781"]}))
    write("dim_status", pd.DataFrame({
        "exception_status": ["Open", "In Review", "Resolved"],
        "status_sort": [1, 2, 3],
        "is_open": [True, True, False]}))
    write("dim_aging_bucket", pd.DataFrame({
        "aging_bucket": ["Current", "1-30", "31-60", "61-90", "91-180", "180+"],
        "bucket_sort": [1, 2, 3, 4, 5, 6],
        "is_past_due": [False, True, True, True, True, True],
        "bucket_band": ["Current", "Early", "Early", "Watch", "Aged", "Aged"]}))

    # ----------------------------------------------------------------- facts
    fi = inv.copy()
    fi["invoice_date_key"] = datekey(fi["invoice_date"])
    fi["due_date_key"] = datekey(fi["due_date"])
    fi["received_date_key"] = datekey(fi["received_date"])
    paid = pay[pay["invoice_id"].notna()].groupby("invoice_id").agg(
        paid_amount_usd=("payment_amount_usd", "sum"),
        payment_count=("payment_id", "count")).reset_index()
    fi = fi.merge(paid, on="invoice_id", how="left").fillna(
        {"paid_amount_usd": 0.0, "payment_count": 0})
    fi["open_amount_usd"] = (fi["invoice_amount_usd"] - fi["paid_amount_usd"]).round(2)
    fi = fi.merge(aging[["invoice_id", "aging_bucket", "days_past_due"]],
                  on="invoice_id", how="left")
    fi["aging_bucket"] = fi["aging_bucket"].fillna("Current")
    fi["days_past_due"] = fi["days_past_due"].fillna(0).astype(int)
    fi["has_exception"] = fi["invoice_id"].isin(set(exc["invoice_id"].dropna()))
    write("fact_invoice", fi[[
        "invoice_id", "invoice_number", "vendor_id", "entity", "gl_account",
        "cost_center", "invoice_date_key", "received_date_key", "due_date_key",
        "payment_terms", "currency", "net_amount", "tax_amount", "invoice_amount",
        "fx_rate", "invoice_amount_usd", "paid_amount_usd", "open_amount_usd",
        "payment_count", "invoice_status", "aging_bucket", "days_past_due",
        "has_exception"]])

    fp = pay.copy()
    fp["payment_date_key"] = datekey(fp["payment_date"])
    fp["has_exception"] = fp["payment_id"].isin(set(exc["payment_id"].dropna()))
    write("fact_payment", fp[[
        "payment_id", "payment_reference", "vendor_id", "invoice_id", "entity",
        "payment_date_key", "payment_method", "currency", "payment_amount",
        "fx_rate", "payment_amount_usd", "payment_status", "payment_run_id",
        "has_exception"]])

    fg = gl.copy()
    fg["posting_date_key"] = datekey(fg["posting_date"])
    fg["net_amount_usd"] = (fg["debit_usd"] - fg["credit_usd"]).round(2)
    fg["self_approved"] = fg["created_by"] == fg["approved_by"]
    write("fact_gl", fg[[
        "je_id", "je_line", "posting_date_key", "period", "entity", "gl_account",
        "cost_center", "debit_usd", "credit_usd", "net_amount_usd", "source_module",
        "source_doc_id", "je_type", "created_by", "approved_by", "self_approved"]])

    fb = bank.copy()
    fb["value_date_key"] = datekey(fb["value_date"])
    fb["matched_to_gl"] = fb["bank_reference"].isin(set(pay["payment_reference"]))
    write("fact_bank_transaction", fb[[
        "bank_txn_id", "bank_account", "entity", "value_date_key", "amount_usd",
        "direction", "transaction_type", "statement_id", "matched_to_gl"]])

    fe = exc.copy()
    fe["source_date_key"] = datekey(fe["source_date"])
    fe["detected_date_key"] = datekey(fe["detected_date"])
    fe["resolution_date_key"] = datekey(fe["resolution_date"])
    fe["is_open"] = fe["exception_status"] != "Resolved"
    fe["cash_at_risk"] = fe["exception_type"].isin(
        ["DUPLICATE_PAYMENT", "PAYMENT_AMOUNT_MISMATCH", "DUPLICATE_INVOICE",
         "UNAPPLIED_PAYMENT"])
    write("fact_exception", fe[[
        "exception_id", "control_id", "exception_type", "severity", "entity",
        "vendor_id", "invoice_id", "payment_id", "je_id", "source_date_key",
        "detected_date_key", "resolution_date_key", "period",
        "exception_amount_usd", "recovered_amount_usd", "age_days",
        "days_to_resolve", "sla_days", "sla_breached", "exception_status",
        "assigned_queue", "requires_human_review", "is_open", "cash_at_risk"]])

    fa = an.copy()
    write("fact_exception_analysis", fa[[
        "exception_id", "control_id", "exception_type", "severity", "entity",
        "exception_amount_usd", "analysis_source", "confidence",
        "recommended_action", "recovery_expected_usd", "repeat_risk",
        "control_gap", "requires_human_review", "validation_status",
        "blocking_checks", "advisory_checks", "reviewer_disposition"]])

    ft = tie.copy()
    ft["as_of_date_key"] = int(AS_OF.strftime("%Y%m%d"))
    write("fact_ap_tieout", ft)

    # --------------------------------------------------------- relationships
    rels = [
        ("fact_exception", "detected_date_key", "dim_date", "date_key", "Many-to-one", "Active", "Single"),
        ("fact_exception", "source_date_key", "dim_date", "date_key", "Many-to-one", "Inactive", "Single"),
        ("fact_exception", "resolution_date_key", "dim_date", "date_key", "Many-to-one", "Inactive", "Single"),
        ("fact_exception", "control_id", "dim_control", "control_id", "Many-to-one", "Active", "Single"),
        ("fact_exception", "exception_type", "dim_exception_type", "exception_type", "Many-to-one", "Active", "Single"),
        ("fact_exception", "severity", "dim_severity", "severity", "Many-to-one", "Active", "Single"),
        ("fact_exception", "exception_status", "dim_status", "exception_status", "Many-to-one", "Active", "Single"),
        ("fact_exception", "entity", "dim_entity", "entity", "Many-to-one", "Active", "Single"),
        ("fact_exception", "vendor_id", "dim_vendor", "vendor_id", "Many-to-one", "Active", "Single"),
        ("fact_exception_analysis", "exception_id", "fact_exception", "exception_id", "One-to-one", "Active", "Both"),
        ("fact_invoice", "invoice_date_key", "dim_date", "date_key", "Many-to-one", "Active", "Single"),
        ("fact_invoice", "due_date_key", "dim_date", "date_key", "Many-to-one", "Inactive", "Single"),
        ("fact_invoice", "vendor_id", "dim_vendor", "vendor_id", "Many-to-one", "Active", "Single"),
        ("fact_invoice", "entity", "dim_entity", "entity", "Many-to-one", "Active", "Single"),
        ("fact_invoice", "gl_account", "dim_gl_account", "gl_account", "Many-to-one", "Active", "Single"),
        ("fact_invoice", "cost_center", "dim_cost_center", "cost_center", "Many-to-one", "Active", "Single"),
        ("fact_invoice", "aging_bucket", "dim_aging_bucket", "aging_bucket", "Many-to-one", "Active", "Single"),
        ("fact_payment", "payment_date_key", "dim_date", "date_key", "Many-to-one", "Active", "Single"),
        ("fact_payment", "vendor_id", "dim_vendor", "vendor_id", "Many-to-one", "Active", "Single"),
        ("fact_payment", "entity", "dim_entity", "entity", "Many-to-one", "Active", "Single"),
        ("fact_gl", "posting_date_key", "dim_date", "date_key", "Many-to-one", "Active", "Single"),
        ("fact_gl", "gl_account", "dim_gl_account", "gl_account", "Many-to-one", "Active", "Single"),
        ("fact_gl", "cost_center", "dim_cost_center", "cost_center", "Many-to-one", "Active", "Single"),
        ("fact_gl", "entity", "dim_entity", "entity", "Many-to-one", "Active", "Single"),
        ("fact_bank_transaction", "value_date_key", "dim_date", "date_key", "Many-to-one", "Active", "Single"),
        ("fact_bank_transaction", "entity", "dim_entity", "entity", "Many-to-one", "Active", "Single"),
        ("fact_ap_tieout", "entity", "dim_entity", "entity", "Many-to-one", "Active", "Single"),
    ]
    write("relationships", pd.DataFrame(rels, columns=[
        "from_table", "from_column", "to_table", "to_column", "cardinality",
        "state", "cross_filter_direction"]))

    with open(os.path.join(PBI, "model_manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"tables": written, "relationships": len(rels),
                   "date_grain": "day", "as_of_date": AS_OF.date().isoformat(),
                   "measure_file": "measures.dax"}, f, indent=2)

    print("Power BI star schema written to powerbi/")
    for k, v in written.items():
        print(f"  {k:<28} {v:>7,} rows")
    print(f"  {len(rels)} relationships in relationships.csv")


if __name__ == "__main__":
    main()
