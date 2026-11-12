# AI-Assisted Exception Analysis: Design & Controls

The AI layer sits **after** detection and **before** human triage. It does not
decide whether something is an exception — the deterministic controls do that —
and it does not close anything. It assembles the context an analyst would
assemble, proposes a root cause and an action, and submits that proposal to a
validation gate before it is allowed near the register.

```
exception ──▶ evidence pack ──▶ analysis ──▶ validation gate ──▶ register
                 (facts)        (proposal)   (V-01..V-10)          │
                                                   │               ▼
                                             fail ─┴──▶ baseline, flagged
                                                        for manual triage
```

## 1. Why this shape

The reason to be careful is specific: in finance operations a plausible wrong
answer is worse than no answer. An analyst who reads "the vendor was overpaid by
USD 482,119 across two runs" and acts on it has wasted a week if that number was
never in the evidence. So the design assumes the model can be wrong and makes
being wrong detectable:

- **Grounding is checked, not trusted.** Every figure and every document ID in
  the narrative must trace back to the evidence pack.
- **The action space is closed.** Twelve permitted actions, and a policy matrix
  that says which are defensible for which exception type.
- **There is always a deterministic answer to fall back to.** The baseline
  analyser produces the same schema from the control's own logic, so a rejected
  response degrades to something usable rather than to nothing.
- **The AI never has authority.** It annotates. The disposition is a human's,
  recorded, and measured.

## 2. The evidence pack

Assembled per exception from the source systems — the same pull an analyst does
by hand:

| Section | Contents |
|---|---|
| `exception` | Control, type, severity, entity, value, the detection rule in words, the evidence statement, age, owning queue |
| `policy` | Tolerances in force, materiality threshold, write-off ceiling, **the permitted actions for this exception type** |
| `vendor` | Name, category, country, currency, terms, status, risk tier |
| `invoice` | Full document plus every payment applied to it |
| `payment` | Full document including run ID and status |
| `journal_lines` | Every line of the journal, with preparer and approver |
| `bank_transaction` | The statement line, type and narrative |
| `tieout` | For tie-out exceptions, the entity's variance decomposition |

**Deliberately excluded:** `bank_account_last4` and `tax_id`. The analysis never
needs the values — "these two records share a bank account and tax ID" is the
finding; the digits are not. Excluding them at the source is a stronger control
than screening for them in the output, and the screen (V-08) still runs.

The pack is hashed (SHA-256 over its canonical JSON). The hash is the cache key,
the audit key, and the reproducibility test.

## 3. The output schema

Identical for both providers, enforced as a JSON Schema structured output when
the model generates it:

| Field | Type | Purpose |
|---|---|---|
| `exception_id` | string | Must match the item under analysis |
| `root_cause_hypothesis` | string | Under 60 words, stated as a hypothesis |
| `confidence` | number 0–1 | Self-assessed evidential support |
| `evidence_cited` | string[] | What the conclusion rests on |
| `investigation_steps` | string[] (2–5) | Concrete next actions for an analyst |
| `recommended_action` | enum (12) | From the closed list |
| `recovery_expected_usd` | number | Cannot exceed the exception value |
| `repeat_risk` | enum | Low / Medium / High |
| `control_gap` | string | What upstream change would stop this recurring |
| `requires_human_review` | boolean | Forced true by the materiality gate |

## 4. The validation gate

| ID | Check | Blocking | What it catches |
|---|---|---|---|
| V-01 | Schema and type conformance | ✓ | Missing fields, wrong types, wrong exception ID |
| V-02 | Closed-list enums | ✓ | An invented action or risk level |
| V-03 | Confidence bounds and floor | ✓ / advisory | Out-of-range confidence; below 0.55 routes to a human |
| V-04 | Numeric grounding | ✓ | Any figure ≥ 100 that is not within 1% of a value in the pack |
| V-05 | Identifier grounding | ✓ | A document ID that does not exist |
| V-06 | Materiality gate | advisory | Human review dropped on a material or high-severity item — forced back on |
| V-07 | Action policy matrix | ✓ | A permitted-looking action that is wrong for the type; a write-off above USD 250 |
| V-08 | Sensitive-data screen | ✓ | An account number or tax ID value restated in the narrative |
| V-09 | Cross-check vs baseline | ✓ / advisory | Recovery above the exception value (blocking); divergence from the deterministic answer (advisory) |
| V-10 | Reproducibility | — | Evidence hash logged; cached responses make a rerun identical and unbilled |

**Outcomes.** All clear → *Accepted*. Advisory flags only → *Accepted with
flags*, with the flags visible on the record. Any blocking failure → *Rejected
— baseline used*: the deterministic analysis replaces it and the exception is
forced to human review. The gate never repairs an answer and never lets a
failed one through quietly.

Every decision is written to `output/ai_validation_log.csv` with the evidence
hash, the checks that fired, and the notes — so any analysis can be reproduced
and explained months later.

### V-08 is a value screen, not a word screen

The first version flagged the phrase "tax ID" and rejected four of its own
baseline analyses, where naming the match basis was exactly the right thing to
say. The screen now looks for disclosed *values* — a `12-3456789` pattern, four
digits next to "account", an "ending 4417" construction. Over-broad screens get
switched off by the people they annoy; that is a worse outcome than a precise
one.

## 5. Human review routing

An exception is marked for human review when **any** of:

- value ≥ USD 25,000;
- severity Critical or High;
- confidence below 0.55;
- any blocking validation failure.

On the current population that is 61 of 160 (38.1%). The other 61.9% are
auto-routed to a queue with their analysis attached — still worked by a person,
but without a reviewer gate in front of them.

Reviewer dispositions (Accepted / Accepted with edits / Overridden) are recorded
so the layer's usefulness is a measured number, not a claim. On the current
population acceptance is 97.1% with a 2.9% override rate — measured against the
deterministic baseline, which is the honest framing: this is what a rules-based
analyser achieves, and it is the floor any model has to beat.

## 6. Running it against the API

```bash
python3 exception_analysis.py                        # deterministic baseline
python3 exception_analysis.py --provider claude --limit 25
python3 exception_analysis.py --selftest             # the negative tests
```

With `--provider claude` each evidence pack goes to `claude-opus-5-5` as a
structured output against the schema above, with the system prompt cached
across calls and a server-side refusal fallback enabled. Responses are cached
by evidence hash, so a rerun costs nothing and returns the same answer. A
provider error is not a failure mode for the pipeline: the exception falls back
to the baseline analysis and the error is logged.

The default is the deterministic provider precisely because the interesting part
of this design is the gate, not the generation. Anyone can clone this repository
and run the whole thing end to end with no API key and get identical numbers.

## 7. What this layer is worth, stated honestly

- **It does not find exceptions.** The controls do. Removing the AI layer
  changes no balance and no detection figure.
- **What it removes is assembly time** — pulling the invoice, the payments, the
  journal and the vendor record into one view before a human can think. At
  roughly 25 minutes of manual triage per exception, 160 exceptions is about 67
  analyst-hours a period.
- **What it adds is structure** — a root cause taxonomy and a control-gap field
  on every exception, which is what turns an exception register into a
  remediation backlog. The control gaps behind the largest exposure here:
  self-approval on manual journals, an unrestricted AP control account, and no
  duplicate check at vendor creation.
- **What it must never do** is decide. That boundary is in the code, not just in
  the documentation: nothing in this layer writes to `exception_status`.
