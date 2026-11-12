# Exception Management Process

The operating model this pipeline is built to support: who does what, when, and
what has to be true before the books close.

---

## 1. Current state — the problem being solved

A typical mid-market finance operation reconciles AP to the GL once a month,
during close, by exporting both sides to spreadsheets and eyeballing the
difference. That design has four failure modes, all of them visible in this
data set:

| Failure mode | What it looks like here |
|---|---|
| **Detection comes too late** | Exceptions sit an average of 12.8 days before resolution, and the oldest open item is over five months past its transaction date. A duplicate payment found at month end has usually already cleared the bank. |
| **Coverage is partial** | Spreadsheet reconciliation compares totals. It does not catch a self-approved journal, a vendor master duplicate, or an accrual translated at the wrong rate — three control families worth USD 1.45M here. |
| **The variance is unexplained** | "AP is off by USD 314,871" is where most monthly reconciliations stop. Nobody can say how much is missing postings versus topside entries. |
| **Triage is the bottleneck** | 160 exceptions at roughly 25 minutes of manual investigation each is about 67 analyst-hours per period, most of it spent assembling context rather than deciding anything. |

## 2. Future state — the pipeline

```
     NIGHTLY                           DAILY                     MONTHLY
  ┌──────────────┐              ┌────────────────┐         ┌─────────────────┐
  │ Source feeds │              │ Exception      │         │ Close tie-out   │
  │ AP · CM · GL │─ controls ──▶│ register       │────────▶│ C-11 + sign-off │
  │ · bank       │   C-01..C-10 │ + AI triage    │ queues  │ residual = 0    │
  └──────────────┘   C-12, C-13 │ + validation   │         └─────────────────┘
                                └────────────────┘
                                        │
                                   Power BI
                              (controller + queues)
```

**Nightly (02:00).** Source extracts land. The 13 controls run. New exceptions
are written to the register with severity, owning queue and SLA. The AP control
tie-out runs and its variance is decomposed.

**Nightly (02:30).** Each new exception is assembled into an evidence pack and
analysed: root cause, confidence, recommended action from a closed list,
expected recovery, repeat risk, control gap. Every analysis passes a ten-check
validation gate. Anything above USD 25,000, anything Critical or High, and
anything below 0.55 confidence is marked for human review; nothing is ever
auto-closed.

**Daily (08:00).** Queues pick up their work from the dashboard, worked
highest-value-first within severity. Dispositions are recorded against the
exception, which is what makes the acceptance rate measurable.

**Monthly (close day −2).** The tie-out is signed off. The requirement is not a
zero variance — it is a **fully explained** variance: every dollar of difference
traced to a named exception, residual zero.

## 3. Exception lifecycle

```
 detected ──▶ triaged ──▶ assigned ──▶ investigated ──▶ actioned ──▶ resolved
     │            │                          │                          │
     │            └── AI analysis +          └── evidence gathered,     └── root cause
     │                validation gate            vendor contacted           recorded,
     │                                                                      control gap
     └── severity, SLA clock starts                                         logged
```

| State | Definition | Exit criteria |
|---|---|---|
| Open | Raised, not yet picked up | Assigned analyst acknowledges |
| In Review | Being investigated | Action taken or escalated |
| Resolved | Corrected, recovered, or supported | Disposition and root cause recorded |

**SLA clock** starts at detection, not at transaction date. 3 / 5 / 10 / 20
business days by severity. 38 of the 55 open exceptions are currently past SLA —
a backlog signal, not a detection problem.

## 4. Ownership

| Queue | Controls | Responsibility |
|---|---|---|
| **AP Operations** | C-01, C-02, C-03, C-05, C-13 | Vendor contact, credit memos, recovery claims, cash application |
| **Treasury & Cash Operations** | C-06 | Bank confirmations, stale-item void/reissue, bank-originated items |
| **GL & Close Team** | C-04, C-07, C-09, C-11 | Correcting journals, interface failures, FX corrections, tie-out |
| **Controllership Review** | C-08, C-10 | Authorisation breaches, topside entries, policy exceptions |
| **Vendor Master Data** | C-12 | Record merges, duplicate prevention at creation |

### RACI

| Activity | AP Ops | Treasury | GL & Close | Controller | Internal Audit |
|---|---|---|---|---|---|
| Run controls | I | I | **R** | A | I |
| Triage and assign | **R** | **R** | **R** | A | I |
| Investigate and act | **R** | **R** | **R** | C | I |
| Approve a write-off | C | C | C | **R/A** | I |
| Sign off the tie-out | C | C | **R** | **A** | C |
| Approve a threshold change | I | I | C | **R/A** | C |
| Review control gaps | C | C | C | **A** | **R** |

## 5. Escalation

| Trigger | Escalates to | Within |
|---|---|---|
| Critical exception raised | Controller, same day | 1 day |
| SOD breach or topside entry | Controller and Internal Audit | 1 day |
| Open past SLA | Queue lead | at breach |
| Open at 2× SLA | Controller | at breach |
| Tie-out residual non-zero at close −2 | Controller; close checklist item fails | immediate |
| Duplicate payment over USD 100,000 | Controller and Treasury; recovery claim opened | 1 day |

## 6. Close calendar

| Day | Task | Owner | Done when |
|---|---|---|---|
| −5 | Final AP cut-off; interface completeness check (C-04) | AP Ops | 0 missing postings |
| −4 | Bank statement load; C-06 both directions | Treasury | All breaks explained |
| −3 | FX rates published and locked; C-09 rerun | GL & Close | 0 rate exceptions |
| −2 | AP control tie-out (C-11) | GL & Close | Residual zero, variance explained |
| −1 | Manual journal review (C-07, C-08, C-10) | Controllership | Every manual journal supported and independently approved |
| 0 | Exception register snapshot; dashboard refresh | GL & Close | Controller sign-off |
| +2 | Root cause and control gap review | Controller + Internal Audit | Remediation actions assigned |

## 7. Controls over the process itself

The pipeline is a control, so it needs its own controls:

| Risk | Control |
|---|---|
| A rule silently changes behaviour | Same controls implemented in pandas and SQL; the build fails on disagreement |
| A control stops firing | Seeded defects give a measurable recall figure every run |
| The variance has an unidentified cause | The tie-out decomposition's residual must be zero |
| The AI layer invents a fact | Ten-check validation gate, numeric and identifier grounding, baseline fallback |
| The AI layer closes something it should not | It cannot: the analysis annotates, a human dispositions |
| Nobody notices a degrading queue | Backlog, MTTR and SLA breach are reported measures, not ad-hoc queries |
| A threshold is changed quietly | Thresholds live in one place in code, documented, and changing one is a controller decision |

## 8. What this would take to run for real

Honest list of what is simulated here and what a production deployment adds:

- **Source connectivity** — ERP and bank extracts on a schedule, with file
  arrival and record-count checks before the controls run.
- **Case management** — the register needs a real workflow tool for assignment,
  comments, attachments and audit history. The CSV here is the data model, not
  the application.
- **Rate limiting and cost control on the AI layer** — batching, caching (the
  evidence-hash cache here is the pattern), and a per-period spend cap.
- **Access control** — row-level security by entity is specified in the
  dashboard spec; queue-level permissions on the case tool.
- **Change management** — threshold and rule changes version-controlled and
  approved, with the detection scorecard rerun as the regression test.
