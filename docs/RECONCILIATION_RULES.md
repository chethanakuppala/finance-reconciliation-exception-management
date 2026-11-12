# Reconciliation Control Catalogue

Thirteen controls producing fourteen exception types, run against the close of
**2026-09-30** over periods 2026-01 to 2026-09. Each control is implemented
twice — in `reconciliation.py` (pandas) and in `sql/reconciliation_queries.sql`
(SQL against `recon.db`) — and the two are tied out on every run.

## Standing tolerances

| Parameter | Value | Why |
|---|---|---|
| Payment vs invoice | greater of **USD 5.00** or **0.5%** | Below this, bank charges and rounding dominate; chasing costs more than it recovers |
| Journal balance | **USD 0.05** | Covers cent-level rounding on FX translation of multi-line journals; anything larger is a real imbalance |
| FX translation | greater of **USD 25** or **1%** | Absorbs rate rounding without masking a wrong rate |
| AP tie-out reporting | **USD 1,000** per entity | Reporting threshold, not an acceptance threshold — the decomposition still has to explain the whole variance |
| Duplicate invoice window | **10 days** | Covers re-keying of the same document across a weekend and a month end |
| Duplicate payment window | **7 days** | Covers one payment cycle |
| Aged item | **90 days** past due | Escalation point for working capital review |
| Write-off ceiling | **USD 250** | Above this an exception needs a disposition, not a write-off |

## Severity and service levels

| Severity | Trigger | SLA |
|---|---|---|
| Critical | ≥ USD 75,000, or any GL-to-sub-ledger variance | 3 business days |
| High | ≥ USD 25,000, or SOD breach, topside entry, duplicate payment | 5 business days |
| Medium | ≥ USD 5,000, or duplicate vendor master | 10 business days |
| Low | everything else | 20 business days |

Type-based floors exist because value is not the only risk: a self-approved
journal is a control breach whatever its size.

---

## The controls

### C-01 · Duplicate invoice detection
**Objective** — the same vendor invoice is not recognised as a payable twice.
**Sources** — AP sub-ledger.
**Rule** — flag an invoice where either
(a) another invoice for the same vendor has the same *normalised* invoice number
(upper-cased, with punctuation and whitespace stripped), or
(b) another invoice for the same vendor has an identical gross amount with
receipt dates within 10 days.
The earlier-received record is treated as the original; the later one is the
exception.
**Exception** — `DUPLICATE_INVOICE`. **Owner** — AP Operations.
**Remediation** — reverse the later accrual, block it from payment, and open a
vendor recovery claim if it has already been paid.
**Known limit** — when the two records are received within a day of each other,
"which one is the duplicate" needs a system entry timestamp the AP extract does
not carry. The pair is always found; the designation can land on the other
record (see `TEST_SCENARIOS.md`, scenario 1).

### C-02 · Duplicate payment detection
**Objective** — cash does not leave twice for one payable.
**Sources** — disbursements, AP sub-ledger.
**Rule** — flag a payment where either
(a) the invoice it applies to already has an earlier payment (instalments on
Partially Paid invoices are excluded by design), or
(b) the same vendor received an identical USD amount within 7 days.
**Exception** — `DUPLICATE_PAYMENT`, minimum severity High. **Owner** — AP Operations.
**Remediation** — confirm both items cleared the bank, request a refund or
credit memo, and add a pre-release duplicate check to the payment proposal.

### C-03 · Unapplied cash
**Objective** — every disbursement relieves a payable.
**Sources** — disbursements, AP sub-ledger.
**Rule** — flag payments with no `invoice_id`, or with one that does not exist
in the sub-ledger. These post to suspense (99999) rather than AP.
**Exception** — `UNAPPLIED_PAYMENT`. **Owner** — AP Operations.
**Remediation** — match the remittance to open items and apply, or recover.

### C-04 · Sub-ledger to GL completeness
**Objective** — every sub-ledger payable is recognised in the GL.
**Sources** — AP sub-ledger, GL.
**Rule** — flag an invoice with no AP control (20100) credit from the AP module.
**Exception** — `MISSING_GL_POSTING`. **Owner** — GL & Close.
**Remediation** — re-post the accrual in the open period; add a daily
document-count control on the interface.

### C-05 · Payment to invoice agreement
**Objective** — what is paid matches what was agreed.
**Sources** — disbursements, AP sub-ledger.
**Rule** — for each applied payment, flag `|payment − invoice|` above tolerance.
Partially Paid invoices are out of scope: instalments are expected.
**Exception** — `PAYMENT_AMOUNT_MISMATCH`. **Owner** — AP Operations.
**Remediation** — obtain the credit memo behind a short pay, or recover an
overpayment.

### C-06 · Bank to GL cash reconciliation (two directions)
**Objective** — GL cash agrees to the bank statement.
**Sources** — disbursements, GL, bank statements.
**Rule (a)** — a payment with a GL cash credit and no statement line on the same
reference → `GL_CASH_NOT_ON_STATEMENT`.
**Rule (b)** — a statement line whose reference matches no payment →
`BANK_ITEM_NOT_IN_GL` (bank fees, FX charges, returned items, interest).
**Owner** — Treasury & Cash Operations.
**Remediation** — confirm presentment with the bank and void/reissue stale
items; journalise bank-originated items and map the recurring types.

### C-07 · Journal balance
**Objective** — no journal posts out of balance.
**Sources** — GL.
**Rule** — group by journal; flag `|Σ debits − Σ credits| > USD 0.05`.
**Exception** — `UNBALANCED_JOURNAL`. **Owner** — GL & Close.
**Remediation** — retrieve the upload file, post the missing lines, and add a
pre-posting balance check.

### C-08 · Segregation of duties on manual journals
**Objective** — no one approves their own entry.
**Sources** — GL.
**Rule** — flag journals where `created_by = approved_by`.
**Exception** — `SOD_VIOLATION`, minimum severity High. **Owner** — Controllership.
**Remediation** — obtain after-the-fact independent review, log the breach, and
configure the workflow to block self-approval.

### C-09 · FX translation accuracy
**Objective** — foreign-currency payables are translated at the published rate.
**Sources** — AP sub-ledger, GL, FX rate table.
**Rule** — for non-USD invoices with an accrual, compare the posted AP credit to
`invoice_amount × published monthly average rate for the invoice-date period`.
Flag beyond tolerance.
**Exception** — `FX_RATE_ERROR`. **Owner** — GL & Close.
**Remediation** — post the translation correction to FX gain/loss and remove
rate-override rights at AP entry.
**Policy note** — the translation period follows the **invoice date**, not the
date the document was keyed. Using the receipt date instead raises an exception
on every invoice that crosses a month end — a 33-item false-positive population
in this data set (see `TEST_SCENARIOS.md`, scenario 9).

### C-10 · Topside entries to the AP control account
**Objective** — the AP control account only moves through the sub-ledger.
**Sources** — GL.
**Rule** — flag any AP control movement whose `source_module` is not `AP`.
**Exception** — `TOPSIDE_AP_ENTRY`, minimum severity High. **Owner** — Controllership.
**Remediation** — obtain the close support, reclassify to accrued liabilities if
no payable exists, and restrict manual posting rights on the control account.

### C-11 · AP control tie-out (derived)
**Objective** — the GL AP control account agrees to the AP sub-ledger.
**Sources** — all four.
**Rule** — per entity, compare

```
sub-ledger open = Σ (invoice − payments applied) in document currency
                    × the invoice-date rate
GL AP control   = Σ AP credits − Σ AP debits on account 20100
```

and decompose the variance into its four possible causes: missing GL postings
(C-04), FX posting errors (C-09), topside entries (C-10), and FX movement
between accrual and settlement. Flag an entity above the USD 1,000 threshold.
**Exception** — `GL_SUBLEDGER_VARIANCE`, severity Critical. **Owner** — GL & Close.
**The test that matters** — the unexplained residual. Across all three entities
it is **USD 0.01**, i.e. the exception register accounts for the entire
difference. A non-zero residual means a cause nobody has identified yet.

### C-12 · Duplicate vendor master records
**Objective** — one legal entity, one vendor record.
**Sources** — vendor master.
**Rule** — flag a vendor that shares a bank account *and* tax ID with an earlier
record, or whose normalised name matches an earlier record exactly.
**Exception** — `DUPLICATE_VENDOR_MASTER`, minimum severity Medium.
**Owner** — Vendor Master Data.
**Remediation** — block the newer record, redirect open items, review both
payment histories, and enable duplicate detection at creation.
**Note** — these carry a zero dollar amount by design: the exposure is the
duplicate invoices and payments they enable, which are counted under C-01/C-02.
Counting the value twice would overstate exposure.

### C-13 · Aged open payables (operational)
**Objective** — payables do not sit unresolved past 90 days.
**Sources** — AP sub-ledger, disbursements.
**Rule** — open balance on an invoice more than 90 days past due at close.
**Exception** — `AGED_OPEN_ITEM`. **Owner** — AP Operations.
**Remediation** — confirm the liability, obtain approval to pay or
documentation to release the accrual.

---

## Design decisions worth stating

**Why two implementations.** A control is only as good as its specification. A
rule written once in pandas can quietly drift from what the documentation says;
writing it a second time in SQL and failing the build when the counts differ
turns the specification into something testable. `build_warehouse.py` exits
non-zero on any disagreement.

**Why exclude Partially Paid invoices from C-02 and C-05.** Instalment payments
are legitimate and would otherwise generate a false positive per instalment.
The trade-off is explicit: a genuine duplicate on an instalment invoice would be
missed by rule (a) of C-02, though rule (b) — same vendor, same amount, 7 days —
still covers it.

**Why C-12 carries no dollar amount.** Severity would otherwise be driven by a
value already counted elsewhere.

**Why fuzzy rules sit behind exact rules.** In both C-01 and C-02 the exact rule
runs first and the fuzzy rule skips anything already flagged, so one defect
produces one exception rather than two.

**Where the thresholds came from.** They are the thresholds this model is built
on, documented so they can be argued with. In a real deployment each one is a
policy decision owned by the controller, and the right way to set them is to run
the control at several thresholds and look at the exception population each one
produces.
