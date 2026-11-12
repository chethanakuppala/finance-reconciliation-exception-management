#!/usr/bin/env python3
"""
build_warehouse.py - load the source data into SQLite and run the SQL controls.

  1. Creates recon.db from sql/schema.sql (tables, indexes, reconciliation views)
  2. Loads every CSV in data/ plus output/exceptions.csv
  3. Runs each block in sql/reconciliation_queries.sql
  4. Ties the SQL control counts back to the Python engine's exception counts

Step 4 is the point: the controls are implemented twice, independently - once
in pandas, once in SQL. If the two disagree, one of them has drifted, and the
run fails loudly rather than quietly reporting a different number.

Writes output/sql_query_results.json (every query's rows, capped) and
output/sql_vs_python_tieout.csv.
"""

import csv
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
SQL = os.path.join(ROOT, "sql")
DB = os.path.join(ROOT, "recon.db")

MAX_ROWS_IN_JSON = 50

LOAD_ORDER = [
    ("entities", "entities.csv"),
    ("chart_of_accounts", "chart_of_accounts.csv"),
    ("cost_centers", "cost_centers.csv"),
    ("vendors", "vendors.csv"),
    ("fx_rates", "fx_rates.csv"),
    ("invoices", "invoices.csv"),
    ("payments", "payments.csv"),
    ("gl_entries", "gl_entries.csv"),
    ("bank_transactions", "bank_transactions.csv"),
]


def load_table(con, table, path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        rows = [[r[c] if r[c] != "" else None for c in cols] for r in reader]
    placeholders = ",".join("?" * len(cols))
    con.executemany(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", rows)
    return len(rows)


def parse_queries(path):
    """Split the query file into blocks on the '-- @query:' header."""
    text = open(path, encoding="utf-8").read()
    blocks = []
    parts = re.split(r"(?m)^-- @query:\s*", text)[1:]
    for part in parts:
        lines = part.splitlines()
        qid = lines[0].strip()
        meta = {"query_id": qid, "control_id": "-", "exception_type": "", "name": ""}
        body_start = 1
        for i, ln in enumerate(lines[1:], start=1):
            m = re.match(r"^-- @(control|type|name):\s*(.*)$", ln.strip())
            if m:
                key = {"control": "control_id", "type": "exception_type",
                       "name": "name"}[m.group(1)]
                meta[key] = m.group(2).strip()
                body_start = i + 1
            elif ln.strip().startswith("--"):
                body_start = i + 1
            else:
                break
        sql = "\n".join(lines[body_start:]).strip()
        meta["sql"] = sql
        blocks.append(meta)
    return blocks


def main():
    for needed in [os.path.join(DATA, "invoices.csv"),
                   os.path.join(OUT, "exceptions.csv")]:
        if not os.path.exists(needed):
            sys.exit(f"missing {needed} - run generate_data.py then reconciliation.py first")

    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(SQL, "schema.sql"), encoding="utf-8").read())

    print("loading recon.db")
    loaded = {}
    for table, fname in LOAD_ORDER:
        loaded[table] = load_table(con, table, os.path.join(DATA, fname))
    loaded["exceptions"] = load_table(con, "exceptions",
                                      os.path.join(OUT, "exceptions.csv"))
    con.commit()
    for t, n in loaded.items():
        print(f"  {t:<20} {n:>7,} rows")

    blocks = parse_queries(os.path.join(SQL, "reconciliation_queries.sql"))
    results, sql_counts = [], {}
    print(f"\nrunning {len(blocks)} queries")
    for b in blocks:
        cur = con.execute(b["sql"])
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if b["control_id"] != "-":
            sql_counts[(b["control_id"], b["exception_type"])] = len(rows)
        results.append({
            "query_id": b["query_id"], "control_id": b["control_id"],
            "exception_type": b["exception_type"], "name": b["name"],
            "row_count": len(rows), "columns": cols,
            "rows": [dict(zip(cols, r)) for r in rows[:MAX_ROWS_IN_JSON]],
            "rows_truncated": max(0, len(rows) - MAX_ROWS_IN_JSON),
        })
        print(f"  {b['query_id']}  {b['control_id']:<5} {b['name'][:62]:<62} "
              f"{len(rows):>5} rows")

    # ---- tie out SQL against the Python engine
    py_counts = {}
    with open(os.path.join(OUT, "exceptions.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = (r["control_id"], r["exception_type"])
            py_counts[k] = py_counts.get(k, 0) + 1

    tie_rows, mismatches = [], 0
    for k in sorted(set(py_counts) | set(sql_counts)):
        p, s = py_counts.get(k, 0), sql_counts.get(k, 0)
        ok = p == s
        if not ok:
            mismatches += 1
        tie_rows.append({"control_id": k[0], "exception_type": k[1],
                         "python_engine": p, "sql_engine": s,
                         "difference": s - p, "agrees": ok})

    with open(os.path.join(OUT, "sql_vs_python_tieout.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(tie_rows[0].keys()))
        w.writeheader()
        w.writerows(tie_rows)

    with open(os.path.join(OUT, "sql_query_results.json"), "w", encoding="utf-8") as f:
        json.dump({"database": "recon.db", "queries": results,
                   "sql_vs_python_tieout": tie_rows,
                   "tieout_mismatches": mismatches}, f, indent=2)

    print("\nSQL vs Python control tie-out")
    print(f"  {'control':<7} {'exception type':<26} {'python':>7} {'sql':>7}  agrees")
    for r in tie_rows:
        print(f"  {r['control_id']:<7} {r['exception_type']:<26} "
              f"{r['python_engine']:>7} {r['sql_engine']:>7}  "
              f"{'yes' if r['agrees'] else 'NO  <-- investigate'}")
    con.close()

    if mismatches:
        print(f"\n{mismatches} control(s) disagree between the two implementations.")
        sys.exit(1)
    print("\nall controls agree across both implementations")
    print("wrote recon.db, output/sql_query_results.json, output/sql_vs_python_tieout.csv")


if __name__ == "__main__":
    main()
