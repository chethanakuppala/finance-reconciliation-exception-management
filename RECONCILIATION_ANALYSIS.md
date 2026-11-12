# Reconciliation Findings — Northwind Services Group
**Close of 2026-09-30 · periods 2026-01 to 2026-09 · consolidated USD**

Fourteen exception types run over 1,816 invoices (USD 42.6M), 1,600 payments
(USD 38.7M), 8,779 journal lines and 1,597 bank statement lines across three
entities. 160 exceptions were raised, 105 of them resolved.

> Synthetic data. Every figure below comes from the generated book described in
> `docs/TEST_SCENARIOS.md`. The analysis method is the point; the numbers are
> not a finding about any real company.

---

## 1. Headline

| | |
|---|---:|
| Exceptions raised | **160** (8.8% of invoices) |
| Gross exception value | **USD 6.02M** |
| Still open at close | 55 items, USD 2.83M |
| Cash at risk | **USD 1.60M** |
| Recovered to date | USD 433k |
| AP control variance (3 entities) | **USD 314.9k**, fully explained |
| Unexplained residual | **USD 0.01** |
| Open AP past due | USD 2.35M of USD 5.13M (45.8%) |
| Average days to resolve | 12.8 |

**The USD 6.02M number needs qualifying before anyone quotes it.** It is the sum
of exception amounts across controls, and those amounts are not the same kind of
number. Breaking it apart:

| Category | Value | What the number actually is |
|---|---:|---|
| Cash at risk | USD 1.60M | Money that left, or could leave, without a matching obligation |
| Authorisation findings | USD 2.79M | The **value of journals** posted without independent approval or sub-ledger support — not a loss, an unverified posting |
| Balance misstatement | USD 0.66M | Postings missing, mistranslated or out of balance |
| Bank reconciliation breaks | USD 0.44M | Cash movements not agreed between ledger and statement |
| Aged payables under review | USD 0.53M | Balances open past 90 days, most still owed |

Two of these overlap by construction: the USD 314.9k tie-out variance is caused
by missing postings, FX errors and topside entries that are already counted
individually. Netting that out, the non-overlapping exposure is **USD 5.71M**,
of which **USD 1.60M is cash**. That distinction is the first thing to get right
in an exception report, because "USD 6M of exceptions" and "USD 1.6M of cash at
risk" lead to very different conversations.

---

## 2. Where the money is

### 2.1 Authorisation is the largest single exposure — USD 2.79M across 15 items

Ten manual journals were prepared and approved by the same user (USD 1.42M), and
five topside adjustments moved the AP control account with no sub-ledger
document behind them (USD 1.37M). Fifteen items out of 160 — under 10% of the
population — carry 46% of the gross value.

Neither is a loss. Both are postings nobody independent has verified, which is
exactly the condition under which a loss stays invisible. The topside entries
are also the single largest driver of the balance variance in section 3.

The fix is configuration, not effort: block self-approval in the journal
workflow, and restrict manual posting rights on account 20100. Both are
one-time changes that eliminate the entire category.

### 2.2 Cash at risk — USD 1.60M, and USD 433k already recovered

| Finding | Items | Value |
|---|---:|---:|
| Duplicate payments | 15 | USD 705k |
| Unapplied cash in suspense | 14 | USD 606k |
| Duplicate invoices | 16 | USD 250k |
| Payment amount mismatches | 20 | USD 36k |

The duplicate chain is worth following, because it is one failure with three
symptoms. Four vendor master records were created a second time with the same
bank account and tax ID. Those duplicate records let the same vendor invoice be
keyed again under a different vendor number. Three of those duplicate invoices
were then paid — which is why the duplicate-payment control fired on payments
the duplicate-invoice control had already implicated.

Reading that chain backwards gives the control that actually prevents the
exposure: **a duplicate check on bank account and tax ID at vendor creation**.
It is upstream of USD 955k of duplicate invoice and payment exposure, and it is
the cheapest of the three to implement.

The unapplied cash is a different problem — USD 606k of disbursements sitting in
suspense (99999) with no invoice applied. That balance is not lost, but until it
is applied, the AP sub-ledger overstates what is owed and the suspense account
carries a reconciling item into the audit.

### 2.3 Payment mismatches are small, and that is informative

Twenty mismatches worth USD 36k against USD 38.7M of disbursements — about
0.09%. In a book this size, that says the three-way match is working. It is
worth stating plainly: a control that fires rarely is evidence, not a reason to
retire it.

---

## 3. The AP control tie-out

| Entity | Sub-ledger open | GL AP control | Variance |
|---|---:|---:|---:|
| US01 | 1,086,786 | 1,360,826 | **+274,040** |
| UK01 | 1,941,168 | 1,979,639 | **+38,471** |
| CA01 | 1,453,406 | 1,451,045 | **−2,360** |

Most monthly reconciliations stop at that third column. Decomposing it is the
part that makes the number actionable:

| Component | US01 | UK01 | CA01 |
|---|---:|---:|---:|
| Missing GL postings (C-04) | −143,893 | −148,331 | −2,303 |
| FX posting errors (C-09) | +3,448 | −4,008 | −119 |
| Topside entries (C-10) | +414,924 | +190,807 | 0 |
| FX on settlement | −439 | +3 | +62 |
| **Explained** | **274,040** | **38,471** | **−2,360** |
| **Unexplained residual** | **−0.01** | **+0.03** | **−0.01** |

A residual of one cent across three entities is the real result here. It means
the exception register accounts for the entire difference between the ledger
and the sub-ledger — there is no fourth cause sitting unnamed in the variance.
That is the condition for signing off a tie-out, and it is a materially
different statement from "the variance is within threshold".

The composition also shows why the US01 variance is the largest: USD 414.9k of
topside entries pushing the control account up, partly offset by USD 143.9k of
accruals that never reached the GL pushing it down. Two opposite failures in one
number — which is precisely what netting hides.

---

## 4. Working capital

USD 5.13M of payables open at close, 45.8% of it past due:

| Bucket | Invoices | Balance | Share |
|---|---:|---:|---:|
| Current | 148 | 2,780,154 | 54.2% |
| 1–30 | 50 | 1,025,594 | 20.0% |
| 31–60 | 25 | 543,675 | 10.6% |
| 61–90 | 14 | 253,504 | 4.9% |
| 91–180 | 18 | 223,128 | 4.3% |
| 180+ | 11 | 304,445 | 5.9% |

Twenty-nine invoices worth USD 527.6k are more than 90 days past due, and eleven
of those are past 180 days. An aged payable is not automatically bad — a
disputed invoice *should* sit unpaid — but 180 days means nobody has resolved
the dispute either. These are also the balances most likely to be wrong: the
longer an item sits, the more likely the liability has already been settled,
credited, or was never owed.

The 180+ bucket should be worked as a release-or-pay decision, item by item,
with a disposition recorded on each. That is 11 items.

---

## 5. Operations

| | |
|---|---:|
| Resolved | 105 of 160 (65.6%) |
| Average days to resolve | 12.8 |
| Open past SLA | 38 of 55 |
| Critical open | 12 |

Clearance tracks inflow closely through to July, then diverges: 29 exceptions
raised in September against 5 cleared. Backlog is building at the end of the
period, which is the normal pattern — the most recent exceptions are the ones
nobody has reached yet — but 38 of 55 open items past SLA says the queue is
under-resourced relative to inflow, not that detection is noisy.

Resolution speed splits by control in a way that makes sense: duplicate
payments clear in 4.8 days (a refund request is a well-worn path), while
duplicate invoices take 12.9 (someone has to compare two documents and decide
which is real). The slow controls are the ones that need judgment, which is
where the triage layer in section 6 earns its place.

Workload is concentrated in AP Operations: 94 of 160 exceptions, though
Controllership Review carries the most value (USD 2.79M across 15 items). Those
are different kinds of overload and want different responses — AP Ops needs
throughput, Controllership needs the two configuration changes in section 2.1
so the items stop arriving.

---

## 6. Detection quality and the AI layer

The controls were measured against 125 deliberately injected defects the engine
cannot see:

```
recall (strict)   99.2%     pair-level recall   100%
precision         100%      false positives     0
SQL vs Python     14 of 14 control/type pairs agree
```

The single strict-recall miss is a genuine ambiguity rather than a failure —
with two near-identical invoices received a day apart, naming which one is "the
duplicate" needs an entry timestamp the AP extract does not carry. The pair was
found. `docs/TEST_SCENARIOS.md` sets out the three options and why measuring
both recall figures is the honest answer.

Every exception also carries a structured analysis — root cause, confidence,
recommended action from a closed list, expected recovery, repeat risk, control
gap — which passes a ten-check validation gate before it reaches the register.
38.1% are routed to human review on materiality, severity or low confidence; the
gate rejected none of the deterministic baseline's output, and its own negative
tests (17 of 17) confirm each check fires when it should.

What that layer is worth is assembly time, not detection: 160 exceptions at
roughly 25 minutes of manual context-gathering each is about 67 analyst-hours a
period. What it is not allowed to do is decide — nothing in it writes to the
exception status.

---

## 7. What to do, in order

| # | Action | Addresses | Items | Effort |
|---|---|---|---:|---|
| 1 | Block self-approval on manual journals in the workflow | USD 1.42M of unverified postings | 10 | Configuration |
| 2 | Restrict manual posting rights on AP control account 20100 | USD 1.37M of topside entries; 2/3 of the tie-out variance | 5 | Configuration |
| 3 | Duplicate check on bank account and tax ID at vendor creation | Upstream of USD 955k of duplicate exposure | 20 | Configuration |
| 4 | Pre-release duplicate check in the payment proposal | USD 705k of duplicate payments | 15 | Development |
| 5 | Daily document-count control on the AP-to-GL interface | USD 294.5k of missing accruals | 13 | Development |
| 6 | Remove FX rate override rights at invoice entry | USD 10.1k, and a repeat audit finding risk | 9 | Configuration |
| 7 | Weekly aging of issued-but-uncleared payments, escalate at 30 days | USD 403.5k uncleared, oldest 167 days | 9 | Process |
| 8 | Release-or-pay decision on every item past 180 days | USD 304.4k | 11 | Process |

Items 1–3 are configuration changes that remove the top three categories at
source. They are worth doing before anything is added to the detection side:
a control that stops an exception being created is worth more than a control
that finds it afterwards.

## 8. Limitations

- **Synthetic book.** Volumes, vendor mix and defect rates were chosen to
  exercise every control; a real book would have a different mix and a longer
  tail of edge cases.
- **Thresholds are stated, not calibrated.** Each one is documented and
  defensible but has not been swept against the exception population it
  produces. That sweep is the first thing to run against real data.
- **Workflow state is simulated.** Status, resolution dates and reviewer
  dispositions are generated deterministically, so MTTR, SLA and acceptance
  figures demonstrate the measurement design rather than observed performance.
- **One close.** Nine periods of transactions, one tie-out. Trend claims are
  about this window only.

---

*Method, controls and tolerances: `docs/RECONCILIATION_RULES.md`.
Field-level mappings: `docs/DATA_DICTIONARY.md`.
Control assurance and test results: `docs/TEST_SCENARIOS.md`.
Process, ownership and escalation: `docs/PROCESS_WORKFLOW.md`.
AI design and validation gate: `docs/AI_EXCEPTION_WORKFLOW.md`.*
