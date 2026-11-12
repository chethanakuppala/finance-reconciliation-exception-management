-- =====================================================================
-- schema.sql - reconciliation warehouse (SQLite dialect)
--
-- Six source tables land raw from the simulated systems, three dimension
-- tables describe the ledger, and five views carry the reusable
-- reconciliation logic (open balance, aging, AP control, sub-ledger AP).
-- Loaded by build_warehouse.py into recon.db.
-- =====================================================================

DROP VIEW  IF EXISTS v_ap_tieout;
DROP VIEW  IF EXISTS v_subledger_ap;
DROP VIEW  IF EXISTS v_gl_ap_control;
DROP VIEW  IF EXISTS v_invoice_aging;
DROP VIEW  IF EXISTS v_invoice_balance;
DROP TABLE IF EXISTS exceptions;
DROP TABLE IF EXISTS bank_transactions;
DROP TABLE IF EXISTS gl_entries;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS fx_rates;
DROP TABLE IF EXISTS vendors;
DROP TABLE IF EXISTS cost_centers;
DROP TABLE IF EXISTS chart_of_accounts;
DROP TABLE IF EXISTS entities;

-- ------------------------------------------------------------ dimensions

CREATE TABLE entities (
    entity            TEXT PRIMARY KEY,
    entity_name       TEXT NOT NULL,
    ledger_currency   TEXT NOT NULL
);

CREATE TABLE chart_of_accounts (
    gl_account        TEXT PRIMARY KEY,
    account_name      TEXT NOT NULL,
    account_class     TEXT NOT NULL,    -- Asset / Liability / Expense
    account_group     TEXT NOT NULL     -- Cash / AP Control / Tax / ...
);

CREATE TABLE cost_centers (
    cost_center       TEXT PRIMARY KEY,
    cost_center_name  TEXT NOT NULL
);

CREATE TABLE vendors (
    vendor_id         TEXT PRIMARY KEY,
    vendor_name       TEXT NOT NULL,
    vendor_category   TEXT,
    country           TEXT,
    currency          TEXT,
    payment_terms     TEXT,
    bank_account_last4 TEXT,
    vendor_status     TEXT,
    risk_tier         TEXT,
    onboarded_date    TEXT,
    tax_id            TEXT
);

CREATE TABLE fx_rates (
    period            TEXT NOT NULL,
    from_currency     TEXT NOT NULL,
    to_currency       TEXT NOT NULL,
    rate_type         TEXT NOT NULL,
    fx_rate           REAL NOT NULL,
    effective_date    TEXT,
    rate_source       TEXT,
    PRIMARY KEY (period, from_currency, to_currency, rate_type)
);

-- ---------------------------------------------------------- source facts

CREATE TABLE invoices (
    invoice_id        TEXT PRIMARY KEY,
    invoice_number    TEXT NOT NULL,
    vendor_id         TEXT NOT NULL REFERENCES vendors(vendor_id),
    entity            TEXT NOT NULL REFERENCES entities(entity),
    po_number         TEXT,
    invoice_date      TEXT NOT NULL,
    received_date     TEXT NOT NULL,
    due_date          TEXT NOT NULL,
    payment_terms     TEXT,
    currency          TEXT NOT NULL,
    net_amount        REAL NOT NULL,
    tax_amount        REAL NOT NULL,
    invoice_amount    REAL NOT NULL,
    fx_rate           REAL NOT NULL,
    invoice_amount_usd REAL NOT NULL,
    gl_account        TEXT REFERENCES chart_of_accounts(gl_account),
    cost_center       TEXT REFERENCES cost_centers(cost_center),
    invoice_status    TEXT NOT NULL,     -- Paid / Open / Partially Paid / Disputed
    source_system     TEXT,
    entered_by        TEXT,
    approved_by       TEXT
);

CREATE TABLE payments (
    payment_id        TEXT PRIMARY KEY,
    payment_reference TEXT NOT NULL,
    vendor_id         TEXT NOT NULL REFERENCES vendors(vendor_id),
    invoice_id        TEXT,              -- nullable: unapplied / on-account cash
    entity            TEXT NOT NULL REFERENCES entities(entity),
    payment_date      TEXT NOT NULL,
    payment_method    TEXT,
    currency          TEXT NOT NULL,
    payment_amount    REAL NOT NULL,
    fx_rate           REAL NOT NULL,
    payment_amount_usd REAL NOT NULL,
    bank_account      TEXT,
    payment_run_id    TEXT,
    payment_status    TEXT,
    created_by        TEXT,
    approved_by       TEXT
);

CREATE TABLE gl_entries (
    je_id             TEXT NOT NULL,
    je_line           INTEGER NOT NULL,
    posting_date      TEXT NOT NULL,
    period            TEXT NOT NULL,
    entity            TEXT NOT NULL REFERENCES entities(entity),
    gl_account        TEXT NOT NULL REFERENCES chart_of_accounts(gl_account),
    cost_center       TEXT REFERENCES cost_centers(cost_center),
    debit_usd         REAL NOT NULL DEFAULT 0,
    credit_usd        REAL NOT NULL DEFAULT 0,
    source_module     TEXT NOT NULL,     -- AP / MANUAL
    source_doc_id     TEXT,              -- invoice_id, payment_id, or je_id
    je_type           TEXT,              -- System / Manual
    description       TEXT,
    created_by        TEXT,
    approved_by       TEXT,
    PRIMARY KEY (je_id, je_line)
);

CREATE TABLE bank_transactions (
    bank_txn_id       TEXT PRIMARY KEY,
    bank_account      TEXT NOT NULL,
    entity            TEXT NOT NULL REFERENCES entities(entity),
    value_date        TEXT NOT NULL,
    amount_usd        REAL NOT NULL,
    direction         TEXT NOT NULL,     -- Debit (cash out) / Credit (cash in)
    bank_reference    TEXT NOT NULL,
    transaction_type  TEXT,
    description       TEXT,
    statement_id      TEXT
);

-- Output of reconciliation.py, loaded back in so the exception register can be
-- queried, joined and reported alongside the source data.
CREATE TABLE exceptions (
    exception_id      TEXT PRIMARY KEY,
    control_id        TEXT NOT NULL,
    control_name      TEXT,
    exception_type    TEXT NOT NULL,
    severity          TEXT NOT NULL,
    entity            TEXT,
    period            TEXT,
    vendor_id         TEXT,
    invoice_id        TEXT,
    payment_id        TEXT,
    je_id             TEXT,
    bank_reference    TEXT,
    exception_amount_usd REAL,
    recovered_amount_usd REAL,
    source_date       TEXT,
    detected_date     TEXT,
    age_days          INTEGER,
    exception_status  TEXT,
    resolution_date   TEXT,
    resolution_period TEXT,
    days_to_resolve   REAL,
    sla_days          INTEGER,
    sla_breached      TEXT,
    assigned_queue    TEXT,
    root_cause_category TEXT,
    requires_human_review TEXT,
    detection_rule    TEXT,
    evidence          TEXT,
    match_key_type    TEXT,
    match_key         TEXT
);

CREATE INDEX idx_inv_vendor   ON invoices(vendor_id);
CREATE INDEX idx_inv_entity   ON invoices(entity, invoice_status);
CREATE INDEX idx_inv_number   ON invoices(vendor_id, invoice_number);
CREATE INDEX idx_pay_invoice  ON payments(invoice_id);
CREATE INDEX idx_pay_vendor   ON payments(vendor_id, payment_amount_usd);
CREATE INDEX idx_pay_ref      ON payments(payment_reference);
CREATE INDEX idx_gl_doc       ON gl_entries(source_doc_id, gl_account);
CREATE INDEX idx_gl_acct      ON gl_entries(gl_account, entity);
CREATE INDEX idx_bank_ref     ON bank_transactions(bank_reference);
CREATE INDEX idx_exc_control  ON exceptions(control_id, exception_status);

-- ----------------------------------------------------------------- views

-- Open balance per invoice, in document currency and in USD at the
-- invoice-date rate (the rate the accrual was booked at).
CREATE VIEW v_invoice_balance AS
SELECT  i.invoice_id,
        i.vendor_id,
        i.entity,
        i.currency,
        i.invoice_status,
        i.due_date,
        i.invoice_amount,
        COALESCE(p.paid_doc, 0)                              AS paid_amount,
        ROUND(i.invoice_amount - COALESCE(p.paid_doc, 0), 2) AS open_amount,
        ROUND((i.invoice_amount - COALESCE(p.paid_doc, 0)) * i.fx_rate, 2)
                                                             AS open_amount_usd
FROM        invoices i
LEFT JOIN  (SELECT invoice_id, SUM(payment_amount) AS paid_doc
            FROM   payments
            WHERE  invoice_id IS NOT NULL AND invoice_id <> ''
            GROUP BY invoice_id) p
       ON  p.invoice_id = i.invoice_id;

-- Aging of everything still open at the close date.
CREATE VIEW v_invoice_aging AS
SELECT  b.*,
        CAST(julianday('2026-09-30') - julianday(b.due_date) AS INTEGER) AS days_past_due,
        CASE
            WHEN julianday('2026-09-30') - julianday(b.due_date) <= 0   THEN 'Current'
            WHEN julianday('2026-09-30') - julianday(b.due_date) <= 30  THEN '1-30'
            WHEN julianday('2026-09-30') - julianday(b.due_date) <= 60  THEN '31-60'
            WHEN julianday('2026-09-30') - julianday(b.due_date) <= 90  THEN '61-90'
            WHEN julianday('2026-09-30') - julianday(b.due_date) <= 180 THEN '91-180'
            ELSE '180+'
        END AS aging_bucket
FROM    v_invoice_balance b
WHERE   b.invoice_status <> 'Paid'
  AND   b.open_amount_usd > 0.01;

-- GL side of the AP control account.
CREATE VIEW v_gl_ap_control AS
SELECT  entity,
        ROUND(SUM(credit_usd) - SUM(debit_usd), 2) AS gl_ap_control_usd
FROM    gl_entries
WHERE   gl_account = '20100'
GROUP BY entity;

-- Sub-ledger side of the AP control account.
CREATE VIEW v_subledger_ap AS
SELECT  entity,
        ROUND(SUM(open_amount_usd), 2) AS subledger_ap_open_usd
FROM    v_invoice_balance
GROUP BY entity;

-- The tie-out itself.
CREATE VIEW v_ap_tieout AS
SELECT  s.entity,
        s.subledger_ap_open_usd,
        g.gl_ap_control_usd,
        ROUND(g.gl_ap_control_usd - s.subledger_ap_open_usd, 2) AS variance_usd
FROM        v_subledger_ap s
JOIN        v_gl_ap_control g ON g.entity = s.entity;
