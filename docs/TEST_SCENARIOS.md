# Test Scenarios & Control Assurance

How this project proves the controls work, rather than asserting it.

Three independent pieces of evidence:

1. **Seeded defects.** `generate_data.py` injects 125 known defects across 11
   controls and logs every one to `data/seeded_defects.csv`. The engine never
   reads that log. Afterwards it is used to measure recall and precision per
   control.
2. **Two implementations.** The same controls run in pandas and in SQL. Any
   disagreement fails the build.
3. **A validated tie-out.** The AP control variance is decomposed back to the
   exceptions that caused it. The residual is USD 0.01 across three entities.

Plus a fourth for the AI layer: 17 negative tests that prove each validation
check fires (`exception_analysis.py --selftest`).

---

## 1. Seeded defect scenarios

| # | Control | Scenario injected | Count | Expected detection |
|---|---|---|---:|---|
| 1 | C-01 | A paid invoice is re-keyed under a cosmetically different number — punctuation removed, a leading zero, trailing whitespace, lower case, or an `-A` suffix | 16 | Normalised-number rule, or the amount/date rule for the `-A` variant |
| 2 | C-02 | An already-settled invoice is paid a second time 1–9 days later | 12 | Second payment on a settled invoice |
| 3 | C-03 | Cash released with no invoice reference, posted to suspense | 14 | No resolvable invoice application |
| 4 | C-04 | The accrual journal for a sub-ledger invoice never reaches the GL | 13 | No AP control credit for the document |
| 5 | C-05 | A payment is made at 80–97.5% (short pay) or 102–118% (overpay) of the invoice | 20 | Variance beyond the greater of USD 5.00 or 0.5% |
| 6a | C-06 | A GL cash disbursement never clears the bank | 9 | Payment reference absent from the statement |
| 6b | C-06 | Bank fee, FX charge, returned ACH, interest or adjustment with no GL entry | 6 | Statement reference absent from payments |
| 7 | C-07 | A manual accrual upload posts with 2–22% of its credit side missing | 7 | Journal-level imbalance above USD 0.05 |
| 8 | C-08 | A manual journal is prepared and approved by the same user | 10 | `created_by = approved_by` |
| 9 | C-09 | A foreign-currency accrual is translated at a rate 7–12% off the published rate | 9 | Posted AP vs invoice × published rate |
| 10 | C-10 | A topside close adjustment moves the AP control account with no sub-ledger document | 5 | AP control touched by a non-AP module |
| 11 | C-12 | A vendor is created again with the same bank account and tax ID under a name variant | 4 | Shared bank/tax ID, or identical normalised name |
| | | **Total** | **125** | |

C-11 (tie-out) and C-13 (aged items) have no seeded population: C-11 is derived
from the other controls' effects, and C-13 is an operational review of a
naturally occurring aging tail, not a defect.

## 2. Results

```
recall (strict key match)   99.2%    124 of 125 seeded defects
recall (pair-level)        100.0%    every seeded defect's pair identified
precision                  100.0%    0 false positives across 160 exceptions
SQL vs Python              14 of 14 control/type pairs agree
AP tie-out residual        USD 0.01 across three entities
AI validation self-test    17 of 17 negative cases fire the expected check
```

| Control | Exception type | Seeded | Detected | TP | FN | FP | Recall | Pair recall |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| C-01 | DUPLICATE_INVOICE | 16 | 16 | 15 | 1 | 0 | 93.8% | 100% |
| C-02 | DUPLICATE_PAYMENT | 12 | 15 | 12 | 0 | 0 | 100% | 100% |
| C-03 | UNAPPLIED_PAYMENT | 14 | 14 | 14 | 0 | 0 | 100% | 100% |
| C-04 | MISSING_GL_POSTING | 13 | 13 | 13 | 0 | 0 | 100% | 100% |
| C-05 | PAYMENT_AMOUNT_MISMATCH | 20 | 20 | 20 | 0 | 0 | 100% | 100% |
| C-06 | GL_CASH_NOT_ON_STATEMENT | 9 | 9 | 9 | 0 | 0 | 100% | 100% |
| C-06 | BANK_ITEM_NOT_IN_GL | 6 | 6 | 6 | 0 | 0 | 100% | 100% |
| C-07 | UNBALANCED_JOURNAL | 7 | 7 | 7 | 0 | 0 | 100% | 100% |
| C-08 | SOD_VIOLATION | 10 | 10 | 10 | 0 | 0 | 100% | 100% |
| C-09 | FX_RATE_ERROR | 9 | 9 | 9 | 0 | 0 | 100% | 100% |
| C-10 | TOPSIDE_AP_ENTRY | 5 | 5 | 5 | 0 | 0 | 100% | 100% |
| C-12 | DUPLICATE_VENDOR_MASTER | 4 | 4 | 4 | 0 | 0 | 100% | 100% |

Current figures are regenerated on every run into
`output/detection_scorecard.csv`.

### The one strict-recall miss, explained

Seeded defect `SD-0011` re-keyed `INV-100976` as `INV-90010` with the number
`1014-40757` written as `101440757`. The control found the pair. It named
`INV-100976` as the duplicate because that record was *received* a day later,
while the seeded log names `INV-90010` because that is the record the generator
created second.

This is not a tuning problem, it is a data problem: deciding which of two
near-identical invoices is "the duplicate" needs the system entry timestamp, and
the AP extract carries only a receipt date. Three honest options:

- extend the extract to carry an entry timestamp (the real fix);
- flag both records of the pair and let the analyst pick (more noise, no lost
  information);
- accept it, and measure pair-level recall alongside strict recall — which is
  what this project does, because the investigation starts from the pair either
  way.

### Detections that are not in the seeded log

Three C-02 detections have keys outside the seeded duplicate-payment population:
`PAY-501566`, `PAY-501567`, `PAY-501574`. Each pays a *seeded duplicate invoice*
(`INV-90002`, `INV-90004`, `INV-90014`). The vendor was genuinely paid twice —
once per invoice record — so the control is right, and the duplicate-payment
control has independently corroborated the duplicate-invoice control. The
scorecard counts these as `corroborating_detections` rather than false
positives, and traces each one back to a seeded key to justify the
classification. Blind `detected − seeded = false positives` arithmetic would
have scored 86.7% precision on C-02 and the number would have been wrong.

## 3. Threshold and rule tests

| # | Test | Method | Result |
|---|---|---|---|
| T-1 | Payment tolerance does not fire on rounding | 1,562 payments are in C-05 scope; the 1,542 not seeded with a defect have a maximum absolute difference of 0.00 | 0 false exceptions |
| T-2 | Journal tolerance absorbs FX rounding | FX-scaled journals round per line | 7 detected = 7 seeded; no rounding-driven hits |
| T-3 | Instalments do not read as duplicates | Partially Paid invoices excluded from C-02/C-05 | 24 instalment invoices, 0 false exceptions |
| T-4 | Fuzzy rules do not double-count | Exact rule runs first; fuzzy skips flagged keys | 16 C-01 exceptions from 16 seeded defects |
| T-5 | Bank match is reference-based, not date-based | Items clearing days after the payment date still match on reference | 1,591 of 1,597 statement lines matched; the 6 unmatched are the seeded bank-only items |
| T-6 | Tie-out decomposition is complete | Residual per entity | USD −0.01 / +0.03 / −0.01 |
| T-7 | Determinism | Re-run `generate_data.py` and `reconciliation.py` | Byte-identical outputs (fixed seeds) |

### Scenario 9, in more detail: the FX period basis

The first implementation of C-09 looked up the published rate using the period
of the **receipt** date. It reported 42 exceptions against 9 seeded defects —
33 of them invoices received in a different month from their invoice date,
where the published rate legitimately differs. The rule was not wrong about the
arithmetic; it was wrong about the policy. Translating at the invoice-date
period cut the population to exactly the 9 seeded defects.

The lesson is the ordinary one in reconciliation work: most "false positives"
are an undocumented policy, not a broken rule.

## 4. AI validation self-test

`python3 exception_analysis.py --selftest` feeds deliberately defective
analyses through the validation gate and asserts the expected check fires.
Output: `output/validation_selftest.csv`.

| Check | Negative case | Fires |
|---|---|---|
| V-01 | Required field removed; confidence sent as a string | ✓ |
| V-02 | Action outside the closed list; invalid repeat risk | ✓ |
| V-03 | Confidence of 1.8; confidence below the 0.55 floor | ✓ |
| V-04 | An amount that appears nowhere in the evidence pack | ✓ |
| V-05 | `INV-999999` / `PAY-888888`, which do not exist | ✓ |
| V-06 | Human review dropped on a USD 318,848 critical item | ✓ |
| V-07 | "Merge vendor master records" on a duplicate payment; write-off above the USD 250 ceiling | ✓ |
| V-08 | Bank account value restated; tax ID restated | ✓ |
| V-09 | Recovery estimate three times the exception value; action diverging from baseline | ✓ |
| V-10 | Evidence pack hash stable across rebuilds | ✓ |
| — | A clean baseline analysis passes the whole gate untouched | ✓ |

17 of 17 pass. A control nobody has tested is not a control.

## 5. Reproducing

```bash
python3 generate_data.py        # fixed seed 20260930
python3 reconciliation.py       # exception register + scorecard
python3 build_warehouse.py      # SQL controls; exits 1 on any disagreement
python3 exception_analysis.py --selftest
python3 exception_analysis.py
python3 build_powerbi_model.py
python3 build_dashboard.py
```

Every script is deterministic. Workflow state (status, resolution dates,
reviewer dispositions) is simulated, but simulated from a fixed hash of the
exception ID, so it is stable across runs too.

## 6. What is not tested

- **Performance at scale.** 1,816 invoices and 8,779 journal lines run in under
  a second. Matching behaviour at millions of rows — where the fuzzy rules would
  need blocking keys rather than full group-bys — is not covered.
- **Rule sensitivity.** The thresholds are documented and defensible but have
  not been swept; the exception population at a 0.25% versus 0.5% payment
  tolerance is not measured here.
- **Live LLM output.** The validation gate is tested against constructed
  defective responses. With `--provider claude` it runs against real model
  output, but no claim is made here about how often a live model trips each
  check.
