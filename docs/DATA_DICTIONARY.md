# Data Dictionary & Source-to-Target Mapping

Every field in the pipeline: where it comes from, what it means, and how it maps
into the warehouse and the Power BI model. Four simulated source systems feed
one reconciliation layer.

```
AP sub-ledger ─┐
Disbursements ─┤→  reconciliation.py  →  exception register  →  Power BI
General ledger ─┤   (13 controls)          + tie-out               + dashboard.html
Bank statements ┘   build_warehouse.py     + aging
                    (same controls, SQL)   + detection scorecard
```

Base currency is **USD** for the ledger; documents can be raised in USD, CAD,
GBP, EUR, INR or SGD and are translated at the published monthly average rate
for the invoice period.

---

## 1. Source systems

### 1.1 `data/vendors.csv` — vendor master (AP master data)

| Field | Type | Description | Notes |
|---|---|---|---|
| `vendor_id` | text PK | Vendor number | `V1000`–`V10nn`; `V2xxx` are the duplicate records |
| `vendor_name` | text | Legal / trading name | Normalised for duplicate detection (C-12) |
| `vendor_category` | text | Spend category | Drives the default expense account |
| `country` | text | Remit-to country | Determines billing currency |
| `currency` | text | Billing currency | ISO 4217 |
| `payment_terms` | text | NET15 / NET30 / NET45 / NET60 | Drives `due_date` |
| `bank_account_last4` | text | Last four of remit-to account | **Sensitive** — used by C-12, excluded from the BI model |
| `vendor_status` | text | Active / Inactive | |
| `risk_tier` | text | Low / Medium / High | Vendor risk rating |
| `onboarded_date` | date | Master record creation | Later record is the duplicate in C-12 |
| `tax_id` | text | Tax identification number | **Sensitive** — excluded from the BI model and from AI evidence packs |

### 1.2 `data/invoices.csv` — AP sub-ledger

| Field | Type | Description | Notes |
|---|---|---|---|
| `invoice_id` | text PK | System document number | |
| `invoice_number` | text | Vendor's own invoice number | Free text; the field C-01 normalises |
| `vendor_id` | text FK | → `vendors` | |
| `entity` | text FK | US01 / CA01 / UK01 | |
| `po_number` | text | Purchase order | Blank on ~12% (non-PO spend) |
| `invoice_date` | date | Vendor's invoice date | **Drives the FX translation period** |
| `received_date` | date | Date keyed into AP | Posting date of the accrual; C-01 ordering key |
| `due_date` | date | `invoice_date` + terms | Aging basis |
| `payment_terms` | text | Copied from vendor | |
| `currency` | text | Document currency | |
| `net_amount` | decimal | Goods/services value | |
| `tax_amount` | decimal | Recoverable input tax | Posted to 13100 |
| `invoice_amount` | decimal | `net_amount + tax_amount` | Gross payable, document currency |
| `fx_rate` | decimal | USD per 1 unit, invoice period | From `fx_rates` |
| `invoice_amount_usd` | decimal | `invoice_amount × fx_rate` | Sub-ledger value in USD |
| `gl_account` | text FK | Expense account | |
| `cost_center` | text FK | Owning cost centre | |
| `invoice_status` | text | Paid / Open / Partially Paid / Disputed | Partially Paid is excluded from C-02 and C-05 by design |
| `source_system` | text | `AP_SUBLEDGER` | |
| `entered_by` / `approved_by` | text | AP users | |

### 1.3 `data/payments.csv` — disbursements

| Field | Type | Description | Notes |
|---|---|---|---|
| `payment_id` | text PK | Payment document | |
| `payment_reference` | text | Bank reference | **The key that matches the bank statement** |
| `vendor_id` | text FK | → `vendors` | |
| `invoice_id` | text FK, nullable | Applied invoice | Blank = unapplied cash (C-03) |
| `entity` | text FK | | |
| `payment_date` | date | Value date in the ledger | |
| `payment_method` | text | ACH / Wire / Check | |
| `currency` | text | Settlement currency | |
| `payment_amount` | decimal | Document currency | Compared to `invoice_amount` in C-05 |
| `fx_rate` | decimal | Rate at payment date | Difference vs the accrual rate is realised FX |
| `payment_amount_usd` | decimal | `payment_amount × fx_rate` | Cash credit in the GL |
| `bank_account` | text | Disbursing account | |
| `payment_run_id` | text | Payment proposal batch | Groups a run for duplicate investigation |
| `payment_status` | text | Cleared / Issued - Not Cleared | |
| `created_by` / `approved_by` | text | | |

### 1.4 `data/gl_entries.csv` — general ledger (one row per journal line)

| Field | Type | Description | Notes |
|---|---|---|---|
| `je_id` + `je_line` | text + int PK | Journal and line number | |
| `posting_date` / `period` | date / text | Posting date and `YYYY-MM` | |
| `entity` | text FK | | |
| `gl_account` | text FK | → `chart_of_accounts` | `20100` is the AP control account |
| `cost_center` | text FK | | |
| `debit_usd` / `credit_usd` | decimal | One is zero on every line | C-07 sums these per journal |
| `source_module` | text | `AP` (sub-ledger interface) or `MANUAL` | `MANUAL` on the AP control account is C-10 |
| `source_doc_id` | text | `invoice_id`, `payment_id` or `je_id` | **The join key back to the sub-ledger** |
| `je_type` | text | System / Manual | |
| `description` | text | Narrative | |
| `created_by` / `approved_by` | text | Preparer and approver | Equal = C-08 |

Posting pattern:

| Event | Debit | Credit |
|---|---|---|
| Invoice accrual | expense (net), 13100 (tax) | 20100 AP control (gross) |
| Vendor payment | 20100 AP control | 10100 Cash |
| FX on settlement | 70000 FX gain/loss | or 70000, depending on direction |
| Unapplied payment | 99999 Suspense | 10100 Cash |

### 1.5 `data/bank_transactions.csv` — bank statements

| Field | Type | Description | Notes |
|---|---|---|---|
| `bank_txn_id` | text PK | Statement line | |
| `bank_account` / `entity` | text | | |
| `value_date` | date | Bank value date | Up to 3 business days after payment |
| `amount_usd` | decimal | Settled amount | |
| `direction` | text | Debit (cash out) / Credit (cash in) | |
| `bank_reference` | text | **Matches `payments.payment_reference`** | The C-06 join key |
| `transaction_type` | text | Vendor Payment / Bank Fee / FX Charge / Returned Item / Interest / Adjustment | |
| `description` | text | Statement narrative | |
| `statement_id` | text | Monthly statement | |

### 1.6 Reference tables

`data/fx_rates.csv` — `period`, `from_currency`, `to_currency`, `rate_type`
(`MONTHLY_AVG`), `fx_rate`, `effective_date`, `rate_source`. The published rate
C-09 tests against.

`data/chart_of_accounts.csv` — `gl_account`, `account_name`, `account_class`,
`account_group`. `data/cost_centers.csv`, `data/entities.csv` — descriptive.

### 1.7 `data/seeded_defects.csv` — ground truth (test artefact, not a source)

`defect_id`, `control_id`, `defect_type`, `key_type`, `key`, `related_key`,
`amount_usd`, `entity`, `injected_date`, `description`. Written by the
generator, read **only** by the detection scorecard after matching is complete.

---

## 2. Reconciliation output

### 2.1 `output/exceptions.csv` — the exception register

| Field | Description |
|---|---|
| `exception_id` | `EXC-00001`+, ordered by source date |
| `control_id` / `control_name` | The control that raised it |
| `exception_type` | Machine-readable finding type (14 values) |
| `severity` | Critical / High / Medium / Low — value ladder with type-based floors |
| `entity`, `period`, `vendor_id`, `invoice_id`, `payment_id`, `je_id`, `bank_reference` | Links back to source records |
| `exception_amount_usd` | Value at risk or misstated |
| `recovered_amount_usd` | Recovered on resolved cash items |
| `source_date` | Date of the underlying transaction |
| `detected_date` | Date the control ran against it |
| `age_days` | Close date minus detected date |
| `exception_status` | Open / In Review / Resolved |
| `resolution_date`, `resolution_period`, `days_to_resolve` | Workflow close-out |
| `sla_days`, `sla_breached` | 3/5/10/20 days by severity |
| `assigned_queue` | Owning team |
| `root_cause_category` | Standard cause taxonomy |
| `requires_human_review` | Materiality / severity gate |
| `detection_rule` | The rule in words — the audit trail |
| `evidence` | Human-readable statement of what was found |
| `match_key_type` / `match_key` | Key used for scorecard matching |

### 2.2 Other outputs

| File | Grain | Contents |
|---|---|---|
| `output/invoice_aging.csv` | invoice | Open balance, days past due, aging bucket |
| `output/gl_vs_subledger.csv` | entity | Tie-out with the four-component variance decomposition |
| `output/detection_scorecard.csv` | control × type | Seeded, detected, TP/FN/FP, recall, pair recall, precision |
| `output/sql_vs_python_tieout.csv` | control × type | Counts from both implementations |
| `output/exception_analysis.csv` | exception | AI triage output and validation result |
| `output/ai_validation_log.csv` | exception | Evidence hash, checks fired, disposition |
| `output/validation_selftest.csv` | test case | Negative tests proving each check fires |
| `output/recon_output.json` | — | Every aggregate the dashboard reads |

---

## 3. Mapping into the BI model

`build_powerbi_model.py` converts the flat files into a star schema. Date fields
become integer `*_date_key` (YYYYMMDD) so the model has one conformed date table.

| Source field | Target table.column | Transformation |
|---|---|---|
| `invoices.invoice_date` | `fact_invoice.invoice_date_key` | `YYYYMMDD` integer |
| `invoices.*` + `payments` | `fact_invoice.paid_amount_usd` | Sum of applied payments |
| derived | `fact_invoice.open_amount_usd` | `invoice_amount_usd − paid_amount_usd` |
| `invoice_aging.aging_bucket` | `fact_invoice.aging_bucket` | Joined on `invoice_id`, Current where absent |
| `exceptions.source_date` | `fact_exception.source_date_key` | Inactive date relationship |
| `exceptions.detected_date` | `fact_exception.detected_date_key` | **Active** date relationship |
| `exceptions.resolution_date` | `fact_exception.resolution_date_key` | Inactive date relationship |
| `gl_entries.debit_usd/credit_usd` | `fact_gl.net_amount_usd` | `debit − credit` |
| `gl_entries.created_by/approved_by` | `fact_gl.self_approved` | Equality flag |
| `payments.payment_reference` | `fact_bank_transaction.matched_to_gl` | Reference exists in payments |
| `vendors.bank_account_last4`, `tax_id` | — | **Dropped**: sensitive, not needed for analysis |
| `detection_scorecard` | `dim_control.recall_pct` etc. | Aggregated to control level |

Relationship definitions are in `powerbi/relationships.csv`; measures in
`powerbi/measures.dax`.
