#!/usr/bin/env python3
"""
build_dashboard.py - render dashboard.html from the reconciliation output.

Reads output/recon_output.json and output/exception_analysis.json and writes a
single self-contained HTML file: no CDN, no build step, no network. Open it in
a browser.

Charts are hand-written SVG. Every chart has a hover tooltip and a "Table"
toggle that shows the same numbers as text, so nothing depends on colour or on
reading a mark. The palette is the validated categorical set; severity uses an
ordinal single-hue ramp because severity is ordered, and the reserved status
colours are used only for status badges, always next to their label.
"""

import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")

# ------------------------------------------------------------------ formatting

def usd(v, dp=0):
    v = float(v)
    s = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{s}${a/1_000_000:,.2f}M"
    if a >= 1_000:
        return f"{s}${a/1_000:,.1f}k"
    return f"{s}${a:,.{dp}f}"


def usd_full(v):
    v = float(v)
    return f"{'-' if v < 0 else ''}${abs(v):,.2f}"


def num(v):
    return f"{int(v):,}"


def blank(v):
    """JSON round-tripped from pandas can carry NaN where a value is absent."""
    return v is None or (isinstance(v, float) and v != v)


def opt_num(v):
    return "&mdash;" if blank(v) else num(v)


def opt_pct(v):
    return "&mdash;" if blank(v) else f"{v}%"


ACRONYMS = {"Sod": "SOD", "Gl": "GL", "Ap": "AP", "Fx": "FX", "Sla": "SLA",
            "Je": "JE", "Usd": "USD"}


def pretty_type(t):
    """DUPLICATE_INVOICE -> Duplicate invoice; keeps finance acronyms upper."""
    words = str(t).replace("_", " ").title().split()
    return " ".join(ACRONYMS.get(w, w) for w in words)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def nice_max(v, steps=4):
    if v <= 0:
        return 1, [0]
    import math
    raw = v / steps
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if mag * m >= raw:
            step = mag * m
            break
    top = step * steps
    return top, [step * i for i in range(steps + 1)]


# ---------------------------------------------------------------------- charts

def hbar(data, *, label_key, value_key, width=1000, row_h=30, label_w=300,
         colour="var(--series-1)", value_fmt=usd, sub_key=None, max_rows=None):
    """Horizontal bars: single series, direct value labels, no legend needed."""
    rows = data[:max_rows] if max_rows else data
    h = row_h * len(rows) + 16
    vmax = max([abs(float(r[value_key])) for r in rows] + [1])
    plot_w = width - label_w - 130
    out = [f'<svg viewBox="0 0 {width} {h}" role="img" class="chart">']
    for i, r in enumerate(rows):
        y = i * row_h + 8
        v = float(r[value_key])
        bw = max(2.0, abs(v) / vmax * plot_w)
        lbl = esc(r[label_key])
        sub = f" · {esc(r[sub_key])}" if sub_key else ""
        tip = f"{lbl}{sub} — {usd_full(v)}"
        out.append(
            f'<text x="{label_w - 12}" y="{y + row_h/2 + 4}" text-anchor="end" '
            f'class="cat">{lbl}</text>'
            f'<g class="mark" data-tip="{esc(tip)}">'
            f'<rect x="{label_w}" y="{y + 4}" width="{bw}" height="{row_h - 12}" '
            f'rx="4" fill="{colour}"></rect>'
            f'<rect x="{label_w}" y="{y}" width="{plot_w + 120}" height="{row_h}" '
            f'fill="transparent"></rect></g>'
            f'<text x="{label_w + bw + 10}" y="{y + row_h/2 + 4}" class="val">'
            f'{value_fmt(v)}</text>')
    out.append("</svg>")
    return f'<div class="cw">{"".join(out)}</div>'


def stacked_cols(periods, series, colours, *, width=1000, height=300,
                 pad_l=70, pad_b=34, pad_t=16):
    """Stacked columns with a 2px surface gap between segments."""
    totals = [sum(s["values"][i] for s in series) for i in range(len(periods))]
    top, ticks = nice_max(max(totals + [1]))
    plot_w, plot_h = width - pad_l - 16, height - pad_b - pad_t
    bw = min(56, plot_w / max(len(periods), 1) * 0.6)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    for t in ticks:
        y = pad_t + plot_h - t / top * plot_h
        out.append(f'<line x1="{pad_l}" y1="{y}" x2="{width-16}" y2="{y}" '
                   f'class="grid"></line>'
                   f'<text x="{pad_l-10}" y="{y+4}" text-anchor="end" class="axis">'
                   f'{num(t)}</text>')
    for i, p in enumerate(periods):
        cx = pad_l + (i + 0.5) * plot_w / len(periods)
        acc = 0.0
        for s, c in zip(series, colours):
            v = s["values"][i]
            if v <= 0:
                continue
            seg = v / top * plot_h
            y = pad_t + plot_h - (acc + v) / top * plot_h
            out.append(
                f'<g class="mark" data-tip="{esc(p)} · {esc(s["name"])}: {num(v)}">'
                f'<rect x="{cx-bw/2}" y="{y}" width="{bw}" '
                f'height="{max(1, seg-2)}" fill="{c}" rx="2"></rect></g>')
            acc += v
        out.append(f'<text x="{cx}" y="{height-12}" text-anchor="middle" '
                   f'class="axis">{esc(p)}</text>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{width-16}" '
               f'y2="{pad_t+plot_h}" class="baseline"></line></svg>')
    return f'<div class="cw">{"".join(out)}</div>'


def line_chart(periods, series, colours, *, width=1000, height=300, pad_l=70,
               pad_b=34, pad_t=16, fmt=num, area=False):
    vmax = max([max(s["values"]) for s in series] + [1])
    top, ticks = nice_max(vmax)
    plot_w, plot_h = width - pad_l - 16, height - pad_b - pad_t
    xs = [pad_l + (i + 0.5) * plot_w / len(periods) for i in range(len(periods))]
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    for t in ticks:
        y = pad_t + plot_h - t / top * plot_h
        out.append(f'<line x1="{pad_l}" y1="{y}" x2="{width-16}" y2="{y}" '
                   f'class="grid"></line>'
                   f'<text x="{pad_l-10}" y="{y+4}" text-anchor="end" class="axis">'
                   f'{fmt(t)}</text>')
    for s, c in zip(series, colours):
        pts = [(xs[i], pad_t + plot_h - s["values"][i] / top * plot_h)
               for i in range(len(periods))]
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if area:
            out.append(f'<path d="{d} L{pts[-1][0]:.1f},{pad_t+plot_h} '
                       f'L{pts[0][0]:.1f},{pad_t+plot_h} Z" fill="{c}" '
                       f'opacity="0.14"></path>')
        out.append(f'<path d="{d}" fill="none" stroke="{c}" stroke-width="2" '
                   f'stroke-linejoin="round"></path>')
        for (x, y), i in zip(pts, range(len(periods))):
            out.append(
                f'<g class="mark" data-tip="{esc(periods[i])} · {esc(s["name"])}: '
                f'{fmt(s["values"][i])}">'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{c}" '
                f'stroke="var(--surface-1)" stroke-width="2"></circle>'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="13" fill="transparent">'
                f'</circle></g>')
    for i, p in enumerate(periods):
        out.append(f'<text x="{xs[i]}" y="{height-12}" text-anchor="middle" '
                   f'class="axis">{esc(p)}</text>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{width-16}" '
               f'y2="{pad_t+plot_h}" class="baseline"></line></svg>')
    return f'<div class="cw">{"".join(out)}</div>'


def cols(labels, values, *, width=1000, height=280, colour="var(--series-1)",
         pad_l=80, pad_b=34, pad_t=20, fmt=usd):
    top, ticks = nice_max(max(values + [1]))
    plot_w, plot_h = width - pad_l - 16, height - pad_b - pad_t
    bw = min(72, plot_w / max(len(labels), 1) * 0.58)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    for t in ticks:
        y = pad_t + plot_h - t / top * plot_h
        out.append(f'<line x1="{pad_l}" y1="{y}" x2="{width-16}" y2="{y}" '
                   f'class="grid"></line>'
                   f'<text x="{pad_l-10}" y="{y+4}" text-anchor="end" class="axis">'
                   f'{fmt(t)}</text>')
    for i, (l, v) in enumerate(zip(labels, values)):
        cx = pad_l + (i + 0.5) * plot_w / len(labels)
        bh = max(1.0, v / top * plot_h)
        y = pad_t + plot_h - bh
        out.append(f'<g class="mark" data-tip="{esc(l)}: {usd_full(v)}">'
                   f'<rect x="{cx-bw/2}" y="{y}" width="{bw}" height="{bh}" rx="4" '
                   f'fill="{colour}"></rect></g>'
                   f'<text x="{cx}" y="{y-7}" text-anchor="middle" class="val">'
                   f'{fmt(v)}</text>'
                   f'<text x="{cx}" y="{height-12}" text-anchor="middle" '
                   f'class="axis">{esc(l)}</text>')
    out.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{width-16}" '
               f'y2="{pad_t+plot_h}" class="baseline"></line></svg>')
    return f'<div class="cw">{"".join(out)}</div>'


def legend(names, colours):
    items = "".join(
        f'<span class="lg"><i style="background:{c}"></i>{esc(n)}</span>'
        for n, c in zip(names, colours))
    return f'<div class="legend">{items}</div>'


def table(headers, rows, *, align=None, cls=""):
    align = align or []
    th = "".join(f'<th{" class=r" if align[i:i+1] == ["r"] else ""}>{esc(h)}</th>'
                 for i, h in enumerate(headers))
    body = []
    for r in rows:
        tds = "".join(
            f'<td{" class=r" if align[i:i+1] == ["r"] else ""}>{c}</td>'
            for i, c in enumerate(r))
        body.append(f"<tr>{tds}</tr>")
    return (f'<div class="tw"><table class="{cls}"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def panel(title, note, body, *, alt=None, pid=None):
    toggle = ""
    if alt:
        toggle = (f'<div class="tog"><button class="on" data-show="c-{pid}">Chart'
                  f'</button><button data-show="t-{pid}">Table</button></div>')
        body = (f'<div id="c-{pid}">{body}</div>'
                f'<div id="t-{pid}" hidden>{alt}</div>')
    return (f'<section class="panel"><div class="ph"><div><h3>{esc(title)}</h3>'
            f'<p class="note">{note}</p></div>{toggle}</div>{body}</section>')


def sev_badge(s):
    return f'<span class="badge s-{s.lower()}">{esc(s)}</span>'


def status_pill(s):
    key = {"Open": "open", "In Review": "review", "Resolved": "done"}.get(s, "open")
    return f'<span class="pill p-{key}">{esc(s)}</span>'


# ------------------------------------------------------------------- the page

SEV_RAMP = ["var(--sev-low)", "var(--sev-med)", "var(--sev-high)", "var(--sev-crit)"]


def build():
    d = json.load(open(os.path.join(OUT, "recon_output.json"), encoding="utf-8"))
    ai = json.load(open(os.path.join(OUT, "exception_analysis.json"), encoding="utf-8"))
    k, meta, pop = d["kpis"], d["meta"], d["population"]
    s = ai["summary"]

    # ---- KPI tiles
    tiles = [
        ("Open exceptions", num(k["open_exceptions"]),
         f"{usd(k['open_exception_value_usd'])} open value", ""),
        ("Total exceptions", num(k["total_exceptions"]),
         f"{k['exception_rate_pct']}% of {num(pop['invoices'])} invoices", ""),
        ("Cash at risk", usd(k["cash_at_risk_usd"]),
         f"{usd(k['recovered_amount_usd'])} recovered to date", "warn"),
        ("Unreconciled AP", usd(k["unreconciled_ap_variance_usd"]),
         f"{usd_full(k['unexplained_residual_usd'])} unexplained", "crit"),
        ("Critical open", num(k["critical_open"]),
         f"{num(k['sla_breached_open'])} open items past SLA", "crit"),
        ("Avg days to resolve", f"{k['avg_days_to_resolve']}",
         f"{num(k['resolved_exceptions'])} items cleared", "good"),
        ("Past due payables", usd(k["past_due_balance_usd"]),
         f"of {usd(k['open_ap_balance_usd'])} open AP", "warn"),
        ("Detection recall", f"{k['detection_recall_pct']}%",
         f"{k['detection_precision_pct']}% precision on "
         f"{num(k['seeded_defects'])} seeded defects", "good"),
    ]
    tile_html = "".join(
        f'<div class="tile {cls}"><span class="tl">{esc(t)}</span>'
        f'<span class="tv">{v}</span><span class="ts">{esc(sub)}</span></div>'
        for t, v, sub, cls in tiles)

    # ---- by control
    ctrl = [r for r in d["by_control"]]
    ctrl_rows = [[f"{esc(r['control_id'])}", esc(pretty_type(r["exception_type"])),
                  num(r["count"]), usd_full(r["value_usd"])] for r in ctrl]
    ctrl_chart = hbar(
        [{"l": f"{r['control_id']}  {pretty_type(r['exception_type'])}",
          "v": r["value_usd"], "n": f"{r['count']} items"} for r in ctrl],
        label_key="l", value_key="v", sub_key="n", row_h=30, label_w=330)

    # ---- severity mix by period
    periods = [t["period"] for t in d["trend"]]
    sev_order = ["Low", "Medium", "High", "Critical"]
    sev_counts = {sv: [0] * len(periods) for sv in sev_order}
    import csv as _csv
    with open(os.path.join(OUT, "exceptions.csv"), encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            if r["period"] in periods and r["severity"] in sev_counts:
                sev_counts[r["severity"]][periods.index(r["period"])] += 1
    sev_series = [{"name": sv, "values": sev_counts[sv]} for sv in sev_order]
    sev_chart = (legend(sev_order, SEV_RAMP) +
                 stacked_cols(periods, sev_series, SEV_RAMP))
    sev_rows = [[p] + [num(sev_counts[sv][i]) for sv in sev_order] +
                [num(sum(sev_counts[sv][i] for sv in sev_order))]
                for i, p in enumerate(periods)]

    # ---- raised vs cleared, backlog
    raised = [t["raised"] for t in d["trend"]]
    cleared = [t["cleared"] for t in d["trend"]]
    flow_chart = (legend(["Raised", "Cleared"],
                         ["var(--series-1)", "var(--series-3)"]) +
                  line_chart(periods,
                             [{"name": "Raised", "values": raised},
                              {"name": "Cleared", "values": cleared}],
                             ["var(--series-1)", "var(--series-3)"]))
    backlog = [b["open_backlog"] for b in d["backlog"]]
    backlog_chart = line_chart(
        periods, [{"name": "Open backlog", "values": backlog}],
        ["var(--series-2)"], area=True, height=240)
    flow_rows = [[p, num(raised[i]), num(cleared[i]), num(backlog[i])]
                 for i, p in enumerate(periods)]

    # ---- aging
    ag = d["aging"]
    aging_chart = cols([r["aging_bucket"] for r in ag],
                       [r["open_balance_usd"] for r in ag])
    aging_rows = [[r["aging_bucket"], num(r["invoices"]),
                   usd_full(r["open_balance_usd"])] for r in ag]

    # ---- tie-out
    tie = d["tieout"]
    tie_rows = []
    for r in tie:
        tie_rows.append([
            esc(r["entity"]), usd_full(r["subledger_ap_open_usd"]),
            usd_full(r["gl_ap_control_usd"]),
            f'<b>{usd_full(r["variance_usd"])}</b>',
            usd_full(r["explained_missing_gl_postings_usd"]),
            usd_full(r["explained_fx_posting_errors_usd"]),
            usd_full(r["explained_topside_entries_usd"]),
            usd_full(r["explained_fx_on_settlement_usd"]),
            f'<span class="{"ok" if abs(float(r["unexplained_residual_usd"])) < 1 else "bad"}">'
            f'{usd_full(r["unexplained_residual_usd"])}</span>'])

    # ---- queue workload
    q_chart = hbar([{"l": r["assigned_queue"], "v": r["value_usd"],
                     "n": f"{r['count']} items"} for r in d["by_queue"]],
                   label_key="l", value_key="v", sub_key="n", label_w=280,
                   colour="var(--series-4)")
    q_rows = [[esc(r["assigned_queue"]), num(r["count"]), usd_full(r["value_usd"])]
              for r in d["by_queue"]]

    # ---- vendors
    vend_rows = [[esc(r["vendor_id"]), esc(r.get("vendor_name", "")),
                  esc(r.get("vendor_category", "")), esc(r.get("risk_tier", "")),
                  num(r["exceptions"]), usd_full(r["value_usd"])]
                 for r in d["top_vendors"]]

    # ---- scorecard
    score_rows = []
    for r in d["scorecard"]:
        note = r.get("note")
        score_rows.append([
            esc(r["control_id"]), esc(r["exception_type"]),
            opt_num(r["seeded"]), num(r["detected"]),
            opt_num(r["true_positives"]), opt_num(r["false_negatives"]),
            opt_num(r["false_positives"]), opt_pct(r["recall_pct"]),
            opt_pct(r.get("pair_recall_pct")), opt_pct(r["precision_pct"]),
            "" if blank(note) else esc(note)])

    # ---- SQL tie-out
    sqlrows = []
    sql_path = os.path.join(OUT, "sql_vs_python_tieout.csv")
    if os.path.exists(sql_path):
        import csv as _csv
        with open(sql_path, encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                sqlrows.append([
                    esc(r["control_id"]), esc(r["exception_type"]),
                    num(r["python_engine"]), num(r["sql_engine"]),
                    f'<span class="{"ok" if r["agrees"] in ("True","true") else "bad"}">'
                    f'{"agrees" if r["agrees"] in ("True","true") else "DIFFERS"}</span>'])

    # ---- AI layer
    ai_tiles = [
        ("Analysed", num(s["analysed"]), "exceptions triaged", ""),
        ("Validation pass rate", f"{s['validation_pass_rate_pct']}%",
         f"{s['rejected_to_baseline']} rejected to baseline", "good"),
        ("Avg confidence", f"{s['avg_confidence']:.2f}",
         f"floor 0.55 routes to a human", ""),
        ("Human review", f"{s['human_review_pct']}%",
         f"{s['auto_routable_pct']}% auto-routable", "warn"),
        ("Expected recovery", usd(s["expected_recovery_usd"]),
         "from duplicates and overpayments", "good"),
        ("Reviewer acceptance",
         f"{s['reviewer_acceptance_pct']}%" if s["reviewer_acceptance_pct"] else "—",
         f"{s['reviewer_override_pct']}% overridden"
         if s["reviewer_override_pct"] is not None else "", ""),
    ]
    ai_tile_html = "".join(
        f'<div class="tile {cls}"><span class="tl">{esc(t)}</span>'
        f'<span class="tv">{v}</span><span class="ts">{esc(sub)}</span></div>'
        for t, v, sub, cls in ai_tiles)
    act_chart = hbar([{"l": r["recommended_action"], "v": r["value_usd"],
                       "n": f"{r['count']} items"} for r in s["by_recommended_action"]],
                     label_key="l", value_key="v", sub_key="n", label_w=300,
                     colour="var(--series-3)")
    act_rows = [[esc(r["recommended_action"]), num(r["count"]),
                 usd_full(r["value_usd"])] for r in s["by_recommended_action"]]
    gap_rows = [[esc(r["control_gap"]), num(r["exceptions"]), usd_full(r["value_usd"])]
                for r in s["by_control_gap"]]

    # ---- largest exceptions
    big_rows = [[esc(r["exception_id"]), esc(r["control_id"]),
                 esc(pretty_type(r["exception_type"])),
                 sev_badge(r["severity"]), esc(r["entity"]),
                 esc(r["vendor_id"] if isinstance(r["vendor_id"], str) else ""),
                 usd_full(r["exception_amount_usd"]),
                 status_pill(r["exception_status"])]
                for r in d["largest_exceptions"]]

    panels = [
        panel("Exception value by control", 
              "Every control that fired, by the dollar value of what it caught. "
              "Authorisation findings carry the largest value; duplicate and "
              "accuracy findings carry the most items.",
              ctrl_chart,
              alt=table(["Control", "Exception type", "Items", "Value"],
                        ctrl_rows, align=["l", "l", "r", "r"]), pid="ctrl"),
        panel("Severity mix by period",
              "Exceptions by the period of the underlying transaction. Severity is "
              "an ordered scale, so it is drawn as one hue stepped light to dark.",
              sev_chart,
              alt=table(["Period"] + sev_order + ["Total"], sev_rows,
                        align=["l", "r", "r", "r", "r", "r"]), pid="sev"),
        panel("Raised against cleared",
              "Inflow is counted on the transaction date, clearance on the "
              "resolution date. The two lines are the same unit, so they share "
              "one axis.",
              flow_chart,
              alt=table(["Period", "Raised", "Cleared", "Open backlog"], flow_rows,
                        align=["l", "r", "r", "r"]), pid="flow"),
        panel("Open exception backlog", 
              "Cumulative raised less cleared at each period end.", backlog_chart),
        panel("AP aging at close",
              "Open payables by days past due. 91-180 and 180+ are the balances "
              "the aged-item control raises as exceptions.",
              aging_chart,
              alt=table(["Bucket", "Invoices", "Open balance"], aging_rows,
                        align=["l", "r", "r"]), pid="age"),
        panel("Exception value by owning queue",
              "Workload distribution across the teams that resolve exceptions.",
              q_chart,
              alt=table(["Queue", "Items", "Value"], q_rows,
                        align=["l", "r", "r"]), pid="queue"),
    ]

    html = f"""<title>Reconciliation Exception Control Tower</title>
<style>
:root {{
  color-scheme: light;
  --surface-1:#fcfcfb; --plane:#f4f4f1; --ink:#0b0b0b; --ink-2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --base:#c3c2b7; --rule:rgba(11,11,11,.10);
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a; --series-4:#eda100;
  --series-5:#e87ba4;
  --sev-low:#86b6ef; --sev-med:#5598e7; --sev-high:#256abf; --sev-crit:#104281;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
  --good-ink:#006300;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --surface-1:#1a1a19; --plane:#0d0d0d; --ink:#f0efec; --ink-2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --base:#383835; --rule:rgba(255,255,255,.10);
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
    --series-5:#d55181;
    --sev-low:#cde2fb; --sev-med:#86b6ef; --sev-high:#3987e5; --sev-crit:#184f95;
    --good-ink:#0ca30c;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface-1:#1a1a19; --plane:#0d0d0d; --ink:#f0efec; --ink-2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --base:#383835; --rule:rgba(255,255,255,.10);
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
  --series-5:#d55181;
  --sev-low:#cde2fb; --sev-med:#86b6ef; --sev-high:#3987e5; --sev-crit:#184f95;
  --good-ink:#0ca30c;
}}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--plane); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-text-size-adjust:100% }}
.wrap {{ max-width:1180px; margin:0 auto; padding:28px 16px 64px }}
header.top {{ margin-bottom:22px }}
h1 {{ font-size:25px; line-height:1.25; margin:0 0 6px; letter-spacing:-.01em }}
.sub {{ color:var(--ink-2); margin:0 0 14px; font-size:14px }}
.synth {{ background:var(--surface-1); border:1px solid var(--rule);
  border-left:3px solid var(--warn); border-radius:8px; padding:11px 14px;
  color:var(--ink-2); font-size:13px; margin:0 }}
h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--muted); margin:34px 0 12px; font-weight:600 }}
.grid {{ display:grid; gap:12px; grid-template-columns:repeat(4,1fr) }}
.grid.g3 {{ grid-template-columns:repeat(3,1fr) }}
.tile {{ background:var(--surface-1); border:1px solid var(--rule);
  border-radius:10px; padding:14px 15px; display:flex; flex-direction:column;
  gap:3px; border-top:3px solid var(--base) }}
.tile.good {{ border-top-color:var(--good) }}
.tile.warn {{ border-top-color:var(--warn) }}
.tile.crit {{ border-top-color:var(--crit) }}
.tl {{ font-size:12px; color:var(--ink-2) }}
.tv {{ font-size:25px; font-weight:650; letter-spacing:-.02em }}
.ts {{ font-size:12px; color:var(--muted) }}
.panel {{ background:var(--surface-1); border:1px solid var(--rule);
  border-radius:10px; padding:16px 18px 14px; margin-bottom:14px }}
.ph {{ display:flex; justify-content:space-between; align-items:flex-start;
  gap:14px; margin-bottom:10px; flex-wrap:wrap }}
.panel h3 {{ margin:0 0 3px; font-size:15px }}
.note {{ margin:0; color:var(--ink-2); font-size:13px; max-width:74ch }}
.cw {{ overflow-x:auto; overflow-y:hidden; margin:0 -2px }}
.chart {{ width:100%; min-width:540px; height:auto; display:block; overflow:visible }}
text.cat {{ font-size:12px; fill:var(--ink-2) }}
text.val {{ font-size:12px; fill:var(--ink-2); font-variant-numeric:tabular-nums }}
text.axis {{ font-size:11px; fill:var(--muted); font-variant-numeric:tabular-nums }}
line.grid {{ stroke:var(--grid); stroke-width:1 }}
line.baseline {{ stroke:var(--base); stroke-width:1 }}
.mark {{ cursor:default }}
.mark:hover rect[rx], .mark:hover circle {{ filter:brightness(1.08) }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; margin:0 0 8px 4px }}
.lg {{ font-size:12px; color:var(--ink-2); display:inline-flex; align-items:center;
  gap:6px }}
.lg i {{ width:11px; height:11px; border-radius:3px; display:inline-block }}
.tog {{ display:flex; gap:0; border:1px solid var(--rule); border-radius:7px;
  overflow:hidden; flex:0 0 auto }}
.tog button {{ font:inherit; font-size:12px; padding:5px 11px; border:0;
  background:transparent; color:var(--ink-2); cursor:pointer }}
.tog button.on {{ background:var(--series-1); color:#fff }}
.tw {{ overflow-x:auto; margin-top:2px }}
table {{ border-collapse:collapse; width:100%; font-size:13px }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--grid);
  white-space:nowrap }}
th {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); font-weight:600 }}
td.r, th.r {{ text-align:right; font-variant-numeric:tabular-nums }}
tbody tr:hover {{ background:var(--plane) }}
table.wrapcell td:first-child {{ white-space:normal; min-width:30ch }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:11px; font-size:11px;
  font-weight:600; color:#fff }}
.s-critical {{ background:var(--crit) }}
.s-high {{ background:var(--serious) }}
.s-medium {{ background:var(--warn); color:#0b0b0b }}
.s-low {{ background:var(--muted) }}
.pill {{ display:inline-block; padding:1px 8px; border-radius:11px; font-size:11px;
  border:1px solid var(--rule) }}
.p-open {{ color:var(--crit) }} .p-review {{ color:var(--serious) }}
.p-done {{ color:var(--good-ink) }}
.ok {{ color:var(--good-ink); font-weight:600 }}
.bad {{ color:var(--crit); font-weight:600 }}
#tip {{ position:fixed; pointer-events:none; opacity:0; transition:opacity .08s;
  background:var(--ink); color:var(--surface-1); font-size:12px; padding:6px 9px;
  border-radius:6px; max-width:320px; z-index:9; line-height:1.35 }}
footer {{ margin-top:34px; padding-top:16px; border-top:1px solid var(--rule);
  color:var(--muted); font-size:12px }}
footer code {{ color:var(--ink-2) }}
@media (max-width:860px) {{
  .grid, .grid.g3 {{ grid-template-columns:repeat(2,1fr) }}
  h1 {{ font-size:21px }} .tv {{ font-size:21px }}
}}
@media (max-width:460px) {{ .grid, .grid.g3 {{ grid-template-columns:1fr }} }}
</style>

<div class="wrap">
<header class="top">
  <h1>Financial Reconciliation &amp; Exception Management</h1>
  <p class="sub">{esc(meta['company'])} · close as of {esc(meta['as_of_date'])} ·
     periods {esc(meta['period_range'])} · {num(pop['invoices'])} invoices,
     {num(pop['payments'])} payments, {num(pop['gl_lines'])} GL lines,
     {num(pop['bank_transactions'])} bank items ·
     {pop['controls_run']} controls</p>
  <p class="synth"><b>Synthetic data.</b> Every vendor, invoice, payment, journal
     and bank line here is generated by <code>generate_data.py</code>. The
     defects the controls find were deliberately injected and logged, which is
     what makes the detection recall and precision figures measurable. No figure
     comes from a real company.</p>
</header>

<h2>Close position</h2>
<div class="grid">{tile_html}</div>

<h2>Where the exceptions are</h2>
{"".join(panels)}

<h2>Balance tie-out</h2>
{panel("AP control account: GL against sub-ledger",
       "The variance is decomposed into the four things that can cause it, all "
       "derived from the ledger. The residual is the test: a zero residual means "
       "the exception register explains the entire difference.",
       table(["Entity", "Sub-ledger open", "GL AP control", "Variance",
              "Missing postings", "FX posting errors", "Topside entries",
              "FX on settlement", "Unexplained residual"],
             tie_rows, align=["l"] + ["r"] * 8))}

<h2>AI-assisted exception triage</h2>
<div class="grid g3">{ai_tile_html}</div>
<div style="height:14px"></div>
{panel("Recommended action by value",
       "Each exception gets a structured analysis - root cause, confidence, "
       "recommended action from a closed list, expected recovery - which then "
       "passes a ten-check validation gate before it reaches the register. "
       f"Source: {esc(s['model'])}.",
       act_chart,
       alt=table(["Recommended action", "Items", "Value"], act_rows,
                 align=["l", "r", "r"]), pid="act")}
{panel("Control gaps behind the exceptions",
       "The remediation backlog: what would have to change upstream for these "
       "exceptions to stop being raised.",
       table(["Control gap", "Exceptions", "Value"], gap_rows,
             align=["l", "r", "r"], cls="wrapcell"))}

<h2>Control assurance</h2>
{panel("Detection scorecard against the seeded defect log",
       "The generator logs every defect it injects. The engine never reads that "
       "log - it is used afterwards to measure recall and precision per control. "
       "Pair recall credits a duplicate pair found when the engine designated the "
       "other record of the pair as the duplicate.",
       table(["Control", "Exception type", "Seeded", "Detected", "TP", "FN", "FP",
              "Recall", "Pair recall", "Precision", "Note"],
             score_rows, align=["l", "l", "r", "r", "r", "r", "r", "r", "r", "r", "l"]))}
{panel("SQL engine against Python engine",
       "The same control set is implemented twice - once in pandas, once in SQL "
       "against recon.db. Agreement on every control is the evidence that neither "
       "implementation has drifted.",
       table(["Control", "Exception type", "Python", "SQL", "Result"],
             sqlrows, align=["l", "l", "r", "r", "l"])) if sqlrows else ""}

<h2>Detail</h2>
{panel("Largest exceptions",
       "The fifteen highest-value items in the register, with workflow status.",
       table(["ID", "Control", "Type", "Severity", "Entity", "Vendor", "Value",
              "Status"], big_rows, align=["l", "l", "l", "l", "l", "l", "r", "l"]))}
{panel("Vendors by exception value",
       "Concentration matters: a vendor with repeat exceptions is usually a master "
       "data or process problem, not bad luck.",
       table(["Vendor", "Name", "Category", "Risk tier", "Exceptions", "Value"],
             vend_rows, align=["l", "l", "l", "l", "r", "r"]))}

<footer>
  Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by
  <code>build_dashboard.py</code> from <code>output/recon_output.json</code> and
  <code>output/exception_analysis.json</code>.
  Tolerances: payment variance greater of $5.00 or 0.5%; journal balance $0.05;
  FX translation greater of $25 or 1%; tie-out reporting threshold $1,000;
  aged item 90 days past due. Severity ladder: critical $75k, high $25k,
  medium $5k.
</footer>
</div>

<div id="tip" role="status" aria-live="polite"></div>
<script>
(function () {{
  var tip = document.getElementById('tip');
  document.addEventListener('mousemove', function (e) {{
    var m = e.target.closest ? e.target.closest('.mark') : null;
    if (!m) {{ tip.style.opacity = 0; return; }}
    tip.textContent = m.getAttribute('data-tip') || '';
    var x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 12);
    var y = Math.max(e.clientY - tip.offsetHeight - 12, 8);
    tip.style.left = x + 'px'; tip.style.top = y + 'px'; tip.style.opacity = 1;
  }}, true);
  document.addEventListener('mouseleave', function () {{ tip.style.opacity = 0; }});
  document.querySelectorAll('.tog').forEach(function (tg) {{
    tg.addEventListener('click', function (e) {{
      var b = e.target.closest('button');
      if (!b) return;
      tg.querySelectorAll('button').forEach(function (o) {{
        o.classList.toggle('on', o === b);
        var el = document.getElementById(o.getAttribute('data-show'));
        if (el) el.hidden = (o !== b);
      }});
    }});
  }});
}})();
</script>
"""
    path = os.path.join(ROOT, "dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote dashboard.html ({os.path.getsize(path)/1024:.0f} KB) - "
          f"{len(panels) + 5} panels, {len(tiles) + len(ai_tiles)} KPI tiles")


if __name__ == "__main__":
    build()
