# Power BI Dashboard Specification
**Report:** Financial Reconciliation & Exception Management
**Model:** `powerbi/` star schema (10 dimensions, 7 facts, 27 relationships)
**Measures:** `powerbi/measures.dax` (75 measures across 8 display folders)
**Grain:** one row per exception; as-of date 2026-09-30; periods 2026-01 to 2026-09

This is the build sheet for the report: every page, every visual, the fields it
binds to, and the question it exists to answer. `dashboard.html` in the repo
root is a working implementation of pages 1-4 for anyone who wants to see the
output without opening Power BI.

---

## 1. Model setup

### Load
Get Data → Folder → select `powerbi/`, then Combine → Transform. Each CSV
becomes a table of the same name. `relationships.csv` and `model_manifest.json`
are documentation, not model tables — exclude them from the load.

### Column typing
| Table | Column | Type / setting |
|---|---|---|
| `dim_date` | `date` | Date → **Mark as date table** |
| `dim_date` | `month_name` | Sort by `month_number` |
| `dim_date` | `period` | Sort by `period_sort` |
| `dim_severity` | `severity` | Sort by `severity_sort` |
| `dim_status` | `exception_status` | Sort by `status_sort` |
| `dim_aging_bucket` | `aging_bucket` | Sort by `bucket_sort` |
| all `*_usd` | | Fixed decimal, 2 dp, `$#,0` display |
| all `*_pct`, `*_%` | | Percentage, 1 dp |

### Relationships
Create exactly what `relationships.csv` lists. Three on `fact_exception` are
**inactive by design**:

| From | To | State | Used by |
|---|---|---|---|
| `fact_exception[detected_date_key]` | `dim_date[date_key]` | Active | default date context |
| `fact_exception[source_date_key]` | `dim_date[date_key]` | Inactive | `Exceptions Raised`, `Raised To Date` |
| `fact_exception[resolution_date_key]` | `dim_date[date_key]` | Inactive | `Exceptions Cleared`, `Cleared To Date` |

Leaving all three active would ambiguate the model; the measures activate the
one they need with `USERELATIONSHIP`. The same pattern applies to
`fact_invoice[due_date_key]` (inactive) versus `invoice_date_key` (active).

### Hide from report view
`fact_*` key columns (`*_date_key`), `dim_*` sort columns, and
`fact_exception[evidence]`-class free text that is only used on the
drill-through page.

---

## 2. Theme

Palette (validated for colour-vision deficiency and for both light and dark
backgrounds; the HTML dashboard uses the same values):

| Role | Hex |
|---|---|
| Categorical 1-5 | `#2a78d6` `#eb6834` `#1baf7a` `#eda100` `#e87ba4` |
| Status good | `#0ca30c` |
| Status warning | `#fab219` |
| Status serious | `#ec835a` |
| Status critical | `#d03b3b` |
| Primary ink / secondary / muted | `#0b0b0b` / `#52514e` / `#898781` |
| Gridline / surface | `#e1e0d9` / `#fcfcfb` |

Rules carried into Power BI: severity always uses the four status colours and
never a categorical slot; no visual uses a second y-axis; every chart with two
or more series carries a legend; data labels are selective, not on every point.

---

## 3. Report pages

### Page 1 — Exception Control Tower
*Who it is for:* Controller and finance operations lead.
*The question:* how much is broken right now, how bad, and who owns it.

| # | Visual | Type | Fields / measures |
|---|---|---|---|
| 1.1 | Open exceptions | Card | `[Open Exceptions]`, subtitle `[Open Exception Value]` |
| 1.2 | Exception value | Card | `[Exception Value]` with `[Exception Value MoM %]` as trend |
| 1.3 | Cash at risk | Card | `[Cash at Risk]`, callout `[Recovery Rate %]` |
| 1.4 | Critical open | Card | `[Critical Open]`, conditional background on `[KPI SLA Colour]` |
| 1.5 | Unreconciled AP | Card | `[AP Variance Absolute]`, subtitle `[Tie-out Status]` |
| 1.6 | Exceptions by control | Horizontal bar | Axis `dim_control[control_name]`, value `[Exception Value]`, tooltip `[Exception Count]`, `[Open Exceptions]` |
| 1.7 | Severity mix | Stacked column | Axis `dim_date[period]`, legend `dim_severity[severity]`, value `[Exception Count]` |
| 1.8 | Raised vs cleared | Line | Axis `dim_date[period]`, lines `[Exceptions Raised]` and `[Exceptions Cleared]` |
| 1.9 | Open backlog | Area | Axis `dim_date[period]`, value `[Open Backlog]` |
| 1.10 | Exception register | Table | `exception_id`, `control_id`, `exception_type`, `severity`, `entity`, `exception_amount_usd`, `age_days`, `exception_status`, `assigned_queue` — sorted by value desc, severity as a colour-coded icon column |

Slicers (one row across the top, applied to the whole page): `dim_date[period]`
(between), `dim_entity[entity_name]`, `dim_severity[severity]`,
`dim_status[exception_status]`, `dim_control[control_family]`.

### Page 2 — Cash Exposure & Recovery
*The question:* where is money actually at risk, and how much has come back.

| # | Visual | Type | Fields / measures |
|---|---|---|---|
| 2.1 | Exposure by type | Waterfall | Category `fact_exception[exception_type]` filtered to `cash_at_risk = TRUE`, value `[Exception Value]` |
| 2.2 | Duplicate payment exposure | Card | `[Duplicate Payment Exposure]` |
| 2.3 | Unapplied cash | Card | `[Unapplied Cash]` |
| 2.4 | Recovered to date | Card | `[Recovered Amount]` with `[Recovery Rate %]` |
| 2.5 | Vendor exposure | Scatter | X `[Invoice Value]`, Y `[Exception Value]`, size `[Exception Count]`, detail `dim_vendor[vendor_name]`, legend `dim_vendor[risk_tier]` — series capped at 3 legend values, rest folded to "Other" |
| 2.6 | Top 15 vendors | Table | `vendor_name`, `vendor_category`, `risk_tier`, `[Invoice Count]`, `[Invoice Value]`, `[Exception Count]`, `[Exception Value]`, `[Exception Rate %]` with data bars |
| 2.7 | Recovery pipeline | Funnel | Stage from `fact_exception_analysis[recommended_action]`, value `[Expected Recovery]` |
| 2.8 | Duplicate root cause | Decomposition tree | Analyse `[Exception Value]`, explain by `exception_type` → `root_cause_category` → `vendor_name` |

### Page 3 — AP Aging & Balance Tie-out
*The question:* what is sitting unpaid, and does the ledger agree with the sub-ledger.

| # | Visual | Type | Fields / measures |
|---|---|---|---|
| 3.1 | Open AP | Card | `[Open AP Balance]` |
| 3.2 | Past due share | Card | `[Past Due %]`, subtitle `[Past Due Balance]` |
| 3.3 | Over 90 days | Card | `[Aged Over 90 Balance]` |
| 3.4 | Weighted days past due | Card | `[Weighted Days Past Due]` |
| 3.5 | Aging ladder | Column | Axis `dim_aging_bucket[aging_bucket]`, value `[Open AP Balance]`, single blue series, direct labels |
| 3.6 | Aging by entity | Matrix | Rows `dim_entity[entity_name]`, columns `dim_aging_bucket[aging_bucket]`, values `[Open AP Balance]`, with row subtotals and a diverging background scale |
| 3.7 | Tie-out by entity | Clustered bar | Axis `dim_entity[entity_name]`, series `[GL AP Control]` and `[Sub-ledger AP Open]` |
| 3.8 | Variance decomposition | Waterfall | Categories from `fact_ap_tieout` columns `explained_missing_gl_postings_usd`, `explained_fx_posting_errors_usd`, `explained_topside_entries_usd`, `explained_fx_on_settlement_usd`, `unexplained_residual_usd` (unpivot in Power Query to a `variance_component` / `amount_usd` pair) |
| 3.9 | Residual check | Card | `[Unexplained Residual]` — target 0, red above ±USD 1 |
| 3.10 | Aged item detail | Table | `invoice_id`, `vendor_name`, `entity`, `invoice_status`, `due_date`, `days_past_due`, `open_amount_usd` |

### Page 4 — Operations & SLA
*The question:* is the exception queue being worked fast enough, and where is it stuck.

| # | Visual | Type | Fields / measures |
|---|---|---|---|
| 4.1 | Avg days to resolve | Card | `[Avg Days to Resolve]`, subtitle `[Median Days to Resolve]` |
| 4.2 | Within SLA | Gauge | `[Within SLA %]`, target 0.9 |
| 4.3 | Open past SLA | Card | `[Open Past SLA]` |
| 4.4 | Clearance rate | Card | `[Clearance Rate %]` |
| 4.5 | Queue workload | Stacked bar | Axis `fact_exception[assigned_queue]`, legend `dim_status[exception_status]`, value `[Exception Count]` |
| 4.6 | Resolution time by control | Bar | Axis `dim_control[control_name]`, value `[Avg Days to Resolve]`, reference line at `[Avg Days to Resolve]` for all controls |
| 4.7 | Backlog aging | Column | Axis bucketed `age_days` (0-5, 6-15, 16-30, 31-60, 60+), value `[Open Exceptions]` |
| 4.8 | SLA breach trend | Line + column | Column `[Exception Count]`, line `[SLA Breach %]` — **two charts side by side, not a dual axis** |
| 4.9 | Escalation list | Table | Open exceptions where `sla_breached = TRUE`, sorted by value desc, with `days_past_sla` as a calculated column |

### Page 5 — AI Triage & Validation Assurance
*The question:* can the AI-generated analysis be trusted, and what did it save.

| # | Visual | Type | Fields / measures |
|---|---|---|---|
| 5.1 | Analysed | Card | `[Analysed Exceptions]` |
| 5.2 | Validation pass rate | Card | `[Validation Pass Rate %]`, subtitle `[Validation Rejections]` |
| 5.3 | Avg confidence | Card | `[Avg Confidence]` |
| 5.4 | Human review required | Card | `[Human Review %]`, subtitle `[Auto-routable %]` |
| 5.5 | Reviewer acceptance | Card | `[Reviewer Acceptance %]`, subtitle `[Reviewer Override %]` |
| 5.6 | Recommended actions | Bar | Axis `recommended_action`, value `[Exception Value]`, tooltip `[Expected Recovery]` |
| 5.7 | Validation outcome by control | Matrix | Rows `dim_control[control_name]`, columns `validation_status`, values `[Analysed Exceptions]` |
| 5.8 | Checks triggered | Bar | From `output/ai_validation_log.csv` loaded as `fact_validation_log`, axis check code, value count |
| 5.9 | Confidence vs value | Scatter | X `[Avg Confidence]`, Y `[Exception Value]`, detail `exception_id`, legend `requires_human_review` |
| 5.10 | Control gaps | Table | `control_gap`, `[Analysed Exceptions]`, `[Exception Value]` — the remediation backlog |

### Drill-through — Exception Detail
Drill-through filter: `fact_exception[exception_id]`. Reached by right-clicking
any exception in a table or chart on pages 1-5.

Contents: the full exception record; the AI analysis (root cause, confidence,
recommended action, investigation steps, validation status, reviewer
disposition); the related invoice, payment and journal lines; and the vendor
card. Back button top-left.

---

## 4. Interactions, formatting, governance

**Cross-filtering.** Cards do not filter other visuals (set to None). Register
tables receive filters but do not emit them. The aging matrix on page 3 emits to
the detail table only.

**Conditional formatting.** `severity` → status palette; `sla_breached` → red
icon; `unexplained_residual_usd` → background scale centred on 0;
`exception_rate_pct` → data bars. Every colour-coded column also carries the
text value, so colour is never the only channel.

**Tooltips.** A report-page tooltip (`tt_exception`) shows control name,
detection rule and the evidence statement for any exception mark.

**Row-level security.**

| Role | Filter |
|---|---|
| `Entity_US` | `dim_entity[entity] = "US01"` |
| `Entity_CA` | `dim_entity[entity] = "CA01"` |
| `Entity_UK` | `dim_entity[entity] = "UK01"` |
| `Controller` | no filter |

RLS on `dim_entity` propagates to every fact through the single-direction
relationships; the bidirectional `fact_exception_analysis` → `fact_exception`
relationship is the one to re-check when testing roles.

**Refresh.** The CSV folder is a stand-in for the production source. In a real
deployment: `fact_gl`, `fact_invoice`, `fact_payment`, `fact_bank_transaction`
incremental on their date key (archive 24 months, refresh 3 months);
`fact_exception` and `fact_exception_analysis` full refresh after the nightly
control run; dimensions full refresh.

**Accessibility.** Tab order set per page (cards → charts → table);
alt text on every visual; a "Show as table" alternative available on each chart;
no information carried by colour alone.
