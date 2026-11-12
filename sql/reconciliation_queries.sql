-- =====================================================================
-- reconciliation_queries.sql
--
-- The same 13 controls as reconciliation.py (14 exception types), written
-- independently in SQL.
-- build_warehouse.py runs every block and ties the SQL exception counts back
-- to the Python engine's counts - two implementations of one control set
-- agreeing is the evidence that neither has drifted.
--
-- Block header format (parsed by build_warehouse.py):
--   -- @query:   Q01
--   -- @control: C-01            (or "-" for reporting queries)
--   -- @type:    DUPLICATE_INVOICE
--   -- @name:    human-readable description
-- =====================================================================


-- @query:   Q01
-- @control: C-01
-- @type:    DUPLICATE_INVOICE
-- @name:    Duplicate invoices - normalised number match, or same amount within 10 days
WITH norm AS (
    SELECT  invoice_id, vendor_id, entity, invoice_number, invoice_date, received_date,
            currency, invoice_amount, invoice_amount_usd,
            UPPER(TRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                invoice_number, '-', ''), ' ', ''), '_', ''), '.', ''), '/', ''))) AS norm_no
    FROM    invoices
),
exact_rule AS (
    SELECT  invoice_id, vendor_id, entity, received_date, invoice_number,
            invoice_amount, invoice_amount_usd, currency,
            ROW_NUMBER() OVER (PARTITION BY vendor_id, norm_no
                               ORDER BY received_date, invoice_id) AS seq,
            FIRST_VALUE(invoice_id) OVER (PARTITION BY vendor_id, norm_no
                               ORDER BY received_date, invoice_id) AS original_id
    FROM    norm
),
exact_hits AS (
    SELECT  invoice_id, vendor_id, entity, received_date, invoice_amount_usd,
            original_id, 'Normalised invoice number match' AS rule_applied
    FROM    exact_rule
    WHERE   seq > 1
),
amount_rule AS (
    SELECT  invoice_id, vendor_id, entity, received_date, invoice_amount_usd,
            LAG(invoice_id)     OVER w AS prev_invoice_id,
            LAG(received_date)  OVER w AS prev_received_date
    FROM    norm
    WINDOW  w AS (PARTITION BY vendor_id, invoice_amount
                  ORDER BY received_date, invoice_id)
),
amount_hits AS (
    SELECT  invoice_id, vendor_id, entity, received_date, invoice_amount_usd,
            prev_invoice_id AS original_id,
            'Vendor + identical amount within 10 days' AS rule_applied
    FROM    amount_rule
    WHERE   prev_invoice_id IS NOT NULL
      AND   julianday(received_date) - julianday(prev_received_date) <= 10
      AND   invoice_id NOT IN (SELECT invoice_id FROM exact_hits)
)
SELECT 'C-01' AS control_id, 'DUPLICATE_INVOICE' AS exception_type, *
FROM   (SELECT * FROM exact_hits UNION ALL SELECT * FROM amount_hits)
ORDER  BY received_date;


-- @query:   Q02
-- @control: C-02
-- @type:    DUPLICATE_PAYMENT
-- @name:    Duplicate payments - second payment on a settled invoice, or same vendor+amount in 7 days
WITH applied AS (
    SELECT  p.payment_id, p.payment_reference, p.vendor_id, p.invoice_id, p.entity,
            p.payment_date, p.payment_amount_usd, i.invoice_status
    FROM    payments p
    JOIN    invoices i ON i.invoice_id = p.invoice_id
    WHERE   p.invoice_id IS NOT NULL AND p.invoice_id <> ''
      AND   i.invoice_status <> 'Partially Paid'
),
invoice_rule AS (
    SELECT  *,
            ROW_NUMBER() OVER (PARTITION BY invoice_id
                               ORDER BY payment_date, payment_id) AS seq,
            FIRST_VALUE(payment_id) OVER (PARTITION BY invoice_id
                               ORDER BY payment_date, payment_id) AS original_id
    FROM    applied
),
invoice_hits AS (
    SELECT  payment_id, vendor_id, invoice_id, entity, payment_date,
            payment_amount_usd, original_id,
            'Second payment against a settled invoice' AS rule_applied
    FROM    invoice_rule
    WHERE   seq > 1
),
amount_rule AS (
    SELECT  payment_id, vendor_id, invoice_id, entity, payment_date, payment_amount_usd,
            LAG(payment_id)   OVER w AS prev_payment_id,
            LAG(payment_date) OVER w AS prev_payment_date
    FROM    payments
    WINDOW  w AS (PARTITION BY vendor_id, ROUND(payment_amount_usd, 2)
                  ORDER BY payment_date, payment_id)
),
amount_hits AS (
    SELECT  payment_id, vendor_id, invoice_id, entity, payment_date, payment_amount_usd,
            prev_payment_id AS original_id,
            'Vendor + identical USD amount within 7 days' AS rule_applied
    FROM    amount_rule
    WHERE   prev_payment_id IS NOT NULL
      AND   julianday(payment_date) - julianday(prev_payment_date) <= 7
      AND   payment_id NOT IN (SELECT payment_id FROM invoice_hits)
)
SELECT 'C-02' AS control_id, 'DUPLICATE_PAYMENT' AS exception_type, *
FROM   (SELECT * FROM invoice_hits UNION ALL SELECT * FROM amount_hits)
ORDER  BY payment_date;


-- @query:   Q03
-- @control: C-03
-- @type:    UNAPPLIED_PAYMENT
-- @name:    Unapplied cash - payment with no resolvable invoice application
SELECT  'C-03' AS control_id, 'UNAPPLIED_PAYMENT' AS exception_type,
        p.payment_id, p.payment_reference, p.vendor_id, p.entity, p.payment_date,
        p.payment_amount_usd, p.payment_method, p.payment_run_id,
        CASE WHEN p.invoice_id IS NULL OR p.invoice_id = ''
             THEN 'No invoice reference on the payment record'
             ELSE 'References an invoice that is not in the sub-ledger'
        END AS rule_applied
FROM    payments p
LEFT JOIN invoices i ON i.invoice_id = p.invoice_id
WHERE   p.invoice_id IS NULL OR p.invoice_id = '' OR i.invoice_id IS NULL
ORDER BY p.payment_amount_usd DESC;


-- @query:   Q04
-- @control: C-04
-- @type:    MISSING_GL_POSTING
-- @name:    Completeness - sub-ledger invoice with no AP control credit in the GL
SELECT  'C-04' AS control_id, 'MISSING_GL_POSTING' AS exception_type,
        i.invoice_id, i.invoice_number, i.vendor_id, i.entity, i.received_date,
        i.invoice_status, i.invoice_amount_usd,
        'No AP accrual journal for this sub-ledger document' AS rule_applied
FROM    invoices i
WHERE   NOT EXISTS (
            SELECT 1 FROM gl_entries g
            WHERE  g.source_doc_id = i.invoice_id
              AND  g.gl_account    = '20100'
              AND  g.source_module = 'AP')
ORDER BY i.invoice_amount_usd DESC;


-- @query:   Q05
-- @control: C-05
-- @type:    PAYMENT_AMOUNT_MISMATCH
-- @name:    Payment vs invoice amount outside tolerance (greater of USD 5.00 or 0.5%)
SELECT  'C-05' AS control_id, 'PAYMENT_AMOUNT_MISMATCH' AS exception_type,
        p.payment_id, p.vendor_id, p.invoice_id, p.entity, p.payment_date,
        i.currency, i.invoice_amount, p.payment_amount,
        ROUND(p.payment_amount - i.invoice_amount, 2)              AS variance_doc,
        ROUND((p.payment_amount - i.invoice_amount) * p.fx_rate, 2) AS variance_usd,
        CASE WHEN p.payment_amount > i.invoice_amount THEN 'Overpaid' ELSE 'Short paid' END
                                                                   AS rule_applied
FROM    payments p
JOIN    invoices i ON i.invoice_id = p.invoice_id
WHERE   i.invoice_status <> 'Partially Paid'
  AND   ABS(p.payment_amount - i.invoice_amount) >
        MAX(5.00, ABS(i.invoice_amount) * 0.005)
ORDER BY ABS((p.payment_amount - i.invoice_amount) * p.fx_rate) DESC;


-- @query:   Q06
-- @control: C-06
-- @type:    GL_CASH_NOT_ON_STATEMENT
-- @name:    Bank reconciliation - GL cash disbursement with no statement line
SELECT  'C-06' AS control_id, 'GL_CASH_NOT_ON_STATEMENT' AS exception_type,
        p.payment_id, p.payment_reference, p.vendor_id, p.entity, p.payment_date,
        p.payment_amount_usd, p.payment_status,
        CAST(julianday('2026-09-30') - julianday(p.payment_date) AS INTEGER)
            AS days_uncleared,
        'Cash credited in the GL, never cleared on the statement' AS rule_applied
FROM    payments p
WHERE   EXISTS (SELECT 1 FROM gl_entries g
                WHERE g.source_doc_id = p.payment_id AND g.gl_account = '10100')
  AND   NOT EXISTS (SELECT 1 FROM bank_transactions b
                    WHERE b.bank_reference = p.payment_reference)
ORDER BY p.payment_amount_usd DESC;


-- @query:   Q07
-- @control: C-06
-- @type:    BANK_ITEM_NOT_IN_GL
-- @name:    Bank reconciliation - statement line with no payment or GL document
SELECT  'C-06' AS control_id, 'BANK_ITEM_NOT_IN_GL' AS exception_type,
        b.bank_txn_id, b.bank_reference, b.entity, b.bank_account, b.value_date,
        b.direction, b.transaction_type, b.amount_usd, b.description,
        'Bank-originated item never journalised' AS rule_applied
FROM    bank_transactions b
WHERE   NOT EXISTS (SELECT 1 FROM payments p
                    WHERE p.payment_reference = b.bank_reference)
ORDER BY b.amount_usd DESC;


-- @query:   Q08
-- @control: C-07
-- @type:    UNBALANCED_JOURNAL
-- @name:    Journal entries where debits do not equal credits
SELECT  'C-07' AS control_id, 'UNBALANCED_JOURNAL' AS exception_type,
        je_id,
        MIN(entity)        AS entity,
        MIN(posting_date)  AS posting_date,
        MIN(je_type)       AS je_type,
        MIN(created_by)    AS created_by,
        MIN(description)   AS description,
        ROUND(SUM(debit_usd), 2)                     AS total_debits_usd,
        ROUND(SUM(credit_usd), 2)                    AS total_credits_usd,
        ROUND(SUM(debit_usd) - SUM(credit_usd), 2)   AS out_of_balance_usd,
        'Journal-level debit/credit imbalance above USD 0.05' AS rule_applied
FROM    gl_entries
GROUP BY je_id
HAVING  ABS(SUM(debit_usd) - SUM(credit_usd)) > 0.05
ORDER BY ABS(SUM(debit_usd) - SUM(credit_usd)) DESC;


-- @query:   Q09
-- @control: C-08
-- @type:    SOD_VIOLATION
-- @name:    Segregation of duties - journal prepared and approved by the same user
SELECT  'C-08' AS control_id, 'SOD_VIOLATION' AS exception_type,
        je_id,
        MIN(entity)       AS entity,
        MIN(posting_date) AS posting_date,
        MIN(je_type)      AS je_type,
        MIN(created_by)   AS user_id,
        MIN(description)  AS description,
        ROUND(SUM(debit_usd), 2) AS journal_value_usd,
        'created_by equals approved_by' AS rule_applied
FROM    gl_entries
GROUP BY je_id
HAVING  MIN(created_by) = MIN(approved_by)
ORDER BY SUM(debit_usd) DESC;


-- @query:   Q10
-- @control: C-09
-- @type:    FX_RATE_ERROR
-- @name:    FX translation - AP accrual not booked at the published period rate
WITH posted AS (
    SELECT  source_doc_id AS invoice_id, ROUND(SUM(credit_usd), 2) AS posted_ap_usd
    FROM    gl_entries
    WHERE   gl_account = '20100' AND source_module = 'AP'
    GROUP BY source_doc_id
)
SELECT  'C-09' AS control_id, 'FX_RATE_ERROR' AS exception_type,
        i.invoice_id, i.vendor_id, i.entity, i.invoice_date, i.currency,
        i.invoice_amount, f.fx_rate AS published_rate,
        ROUND(p.posted_ap_usd / i.invoice_amount, 6) AS implied_rate,
        ROUND(i.invoice_amount * f.fx_rate, 2)       AS expected_ap_usd,
        p.posted_ap_usd,
        ROUND(p.posted_ap_usd - i.invoice_amount * f.fx_rate, 2) AS variance_usd,
        'Posted AP differs from invoice x published rate beyond tolerance' AS rule_applied
FROM        invoices i
JOIN        posted   p ON p.invoice_id = i.invoice_id
JOIN        fx_rates f ON f.from_currency = i.currency
                      AND f.period = strftime('%Y-%m', i.invoice_date)
                      AND f.rate_type = 'MONTHLY_AVG'
WHERE   i.currency <> 'USD'
  AND   ABS(p.posted_ap_usd - i.invoice_amount * f.fx_rate) >
        MAX(25.00, ABS(i.invoice_amount * f.fx_rate) * 0.01)
ORDER BY ABS(p.posted_ap_usd - i.invoice_amount * f.fx_rate) DESC;


-- @query:   Q11
-- @control: C-10
-- @type:    TOPSIDE_AP_ENTRY
-- @name:    Topside entries - AP control movement with no sub-ledger document
SELECT  'C-10' AS control_id, 'TOPSIDE_AP_ENTRY' AS exception_type,
        je_id,
        MIN(entity)       AS entity,
        MIN(posting_date) AS posting_date,
        MIN(created_by)   AS created_by,
        MIN(approved_by)  AS approved_by,
        MIN(description)  AS description,
        ROUND(SUM(credit_usd) - SUM(debit_usd), 2) AS ap_movement_usd,
        'AP control account touched by a non-AP source module' AS rule_applied
FROM    gl_entries
WHERE   gl_account = '20100' AND source_module <> 'AP'
GROUP BY je_id
ORDER BY ABS(SUM(credit_usd) - SUM(debit_usd)) DESC;


-- @query:   Q12
-- @control: C-11
-- @type:    GL_SUBLEDGER_VARIANCE
-- @name:    AP control tie-out by entity, variance above the USD 1,000 threshold
SELECT  'C-11' AS control_id, 'GL_SUBLEDGER_VARIANCE' AS exception_type,
        t.entity,
        e.entity_name,
        t.subledger_ap_open_usd,
        t.gl_ap_control_usd,
        t.variance_usd,
        ROUND(100.0 * t.variance_usd / NULLIF(t.subledger_ap_open_usd, 0), 3)
            AS variance_pct_of_subledger,
        'GL AP control does not agree to the AP sub-ledger' AS rule_applied
FROM        v_ap_tieout t
JOIN        entities e ON e.entity = t.entity
WHERE   ABS(t.variance_usd) > 1000.00
ORDER BY ABS(t.variance_usd) DESC;


-- @query:   Q13
-- @control: C-12
-- @type:    DUPLICATE_VENDOR_MASTER
-- @name:    Duplicate vendor master records - shared bank/tax ID or identical normalised name
WITH v AS (
    SELECT  vendor_id, vendor_name, bank_account_last4, tax_id, onboarded_date,
            vendor_category, risk_tier,
            UPPER(TRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                vendor_name, ' ', ''), ',', ''), '.', ''), '-', ''), '''', ''))) AS norm_name
    FROM    vendors
),
bank_rule AS (
    SELECT  v.*,
            ROW_NUMBER() OVER (PARTITION BY bank_account_last4, tax_id
                               ORDER BY onboarded_date, vendor_id) AS seq,
            FIRST_VALUE(vendor_id) OVER (PARTITION BY bank_account_last4, tax_id
                               ORDER BY onboarded_date, vendor_id) AS original_id
    FROM    v
),
bank_hits AS (
    SELECT vendor_id, vendor_name, original_id, onboarded_date,
           'Shared bank account last4 + tax ID' AS rule_applied
    FROM   bank_rule WHERE seq > 1
),
name_rule AS (
    SELECT  v.*,
            ROW_NUMBER() OVER (PARTITION BY norm_name
                               ORDER BY onboarded_date, vendor_id) AS seq,
            FIRST_VALUE(vendor_id) OVER (PARTITION BY norm_name
                               ORDER BY onboarded_date, vendor_id) AS original_id
    FROM    v
),
name_hits AS (
    SELECT vendor_id, vendor_name, original_id, onboarded_date,
           'Identical normalised vendor name' AS rule_applied
    FROM   name_rule
    WHERE  seq > 1 AND vendor_id NOT IN (SELECT vendor_id FROM bank_hits)
)
SELECT 'C-12' AS control_id, 'DUPLICATE_VENDOR_MASTER' AS exception_type, *
FROM   (SELECT * FROM bank_hits UNION ALL SELECT * FROM name_hits)
ORDER  BY onboarded_date;


-- @query:   Q14
-- @control: C-13
-- @type:    AGED_OPEN_ITEM
-- @name:    Aged open payables more than 90 days past due at close
SELECT  'C-13' AS control_id, 'AGED_OPEN_ITEM' AS exception_type,
        a.invoice_id, a.vendor_id, v.vendor_name, a.entity, a.invoice_status,
        a.currency, a.due_date, a.days_past_due, a.aging_bucket, a.open_amount_usd
FROM        v_invoice_aging a
JOIN        vendors v ON v.vendor_id = a.vendor_id
WHERE   a.days_past_due > 90
ORDER BY a.open_amount_usd DESC;


-- @query:   Q15
-- @control: -
-- @type:    REPORT
-- @name:    Exception register summary by control, with SLA performance
SELECT  control_id, control_name, exception_type,
        COUNT(*)                                                    AS exceptions,
        ROUND(SUM(exception_amount_usd), 2)                         AS value_usd,
        SUM(CASE WHEN exception_status = 'Open'      THEN 1 ELSE 0 END) AS open_count,
        SUM(CASE WHEN exception_status = 'In Review' THEN 1 ELSE 0 END) AS in_review_count,
        SUM(CASE WHEN exception_status = 'Resolved'  THEN 1 ELSE 0 END) AS resolved_count,
        ROUND(AVG(CASE WHEN exception_status = 'Resolved'
                       THEN days_to_resolve END), 1)                AS avg_days_to_resolve,
        ROUND(100.0 * SUM(CASE WHEN sla_breached = 'True' THEN 1 ELSE 0 END)
              / COUNT(*), 1)                                        AS sla_breach_pct,
        ROUND(SUM(recovered_amount_usd), 2)                         AS recovered_usd
FROM    exceptions
GROUP BY control_id, control_name, exception_type
ORDER BY value_usd DESC;


-- @query:   Q16
-- @control: -
-- @type:    REPORT
-- @name:    AP aging pivot by entity
SELECT  a.entity,
        ROUND(SUM(CASE WHEN aging_bucket = 'Current' THEN open_amount_usd ELSE 0 END), 2) AS current_usd,
        ROUND(SUM(CASE WHEN aging_bucket = '1-30'    THEN open_amount_usd ELSE 0 END), 2) AS d1_30_usd,
        ROUND(SUM(CASE WHEN aging_bucket = '31-60'   THEN open_amount_usd ELSE 0 END), 2) AS d31_60_usd,
        ROUND(SUM(CASE WHEN aging_bucket = '61-90'   THEN open_amount_usd ELSE 0 END), 2) AS d61_90_usd,
        ROUND(SUM(CASE WHEN aging_bucket = '91-180'  THEN open_amount_usd ELSE 0 END), 2) AS d91_180_usd,
        ROUND(SUM(CASE WHEN aging_bucket = '180+'    THEN open_amount_usd ELSE 0 END), 2) AS d180_plus_usd,
        ROUND(SUM(open_amount_usd), 2)                                                    AS total_open_usd,
        ROUND(100.0 * SUM(CASE WHEN aging_bucket <> 'Current' THEN open_amount_usd ELSE 0 END)
              / NULLIF(SUM(open_amount_usd), 0), 1)                                       AS past_due_pct
FROM    v_invoice_aging a
GROUP BY a.entity
ORDER BY total_open_usd DESC;


-- @query:   Q17
-- @control: -
-- @type:    REPORT
-- @name:    Vendor exception ranking against invoice spend
WITH spend AS (
    SELECT vendor_id, COUNT(*) AS invoices, ROUND(SUM(invoice_amount_usd), 2) AS spend_usd
    FROM   invoices GROUP BY vendor_id
),
exc AS (
    SELECT vendor_id, COUNT(*) AS exceptions,
           ROUND(SUM(exception_amount_usd), 2) AS exception_value_usd
    FROM   exceptions WHERE vendor_id IS NOT NULL AND vendor_id <> ''
    GROUP BY vendor_id
)
SELECT  v.vendor_id, v.vendor_name, v.vendor_category, v.risk_tier, v.country,
        s.invoices, s.spend_usd, e.exceptions, e.exception_value_usd,
        ROUND(100.0 * e.exceptions / s.invoices, 1)              AS exception_rate_pct,
        ROUND(100.0 * e.exception_value_usd / s.spend_usd, 1)    AS value_at_risk_pct
FROM        vendors v
JOIN        spend   s ON s.vendor_id = v.vendor_id
JOIN        exc     e ON e.vendor_id = v.vendor_id
ORDER BY e.exception_value_usd DESC
LIMIT 15;


-- @query:   Q18
-- @control: -
-- @type:    REPORT
-- @name:    Monthly exception inflow, clearance and running backlog
WITH raised AS (
    SELECT period AS mth, COUNT(*) AS raised,
           ROUND(SUM(exception_amount_usd), 2) AS raised_value_usd
    FROM   exceptions GROUP BY period
),
cleared AS (
    SELECT resolution_period AS mth, COUNT(*) AS cleared
    FROM   exceptions
    WHERE  exception_status = 'Resolved' AND resolution_period IS NOT NULL
    GROUP BY resolution_period
),
cal AS (
    SELECT mth FROM raised UNION SELECT mth FROM cleared
)
SELECT  c.mth AS period,
        COALESCE(r.raised, 0)            AS raised,
        COALESCE(r.raised_value_usd, 0)  AS raised_value_usd,
        COALESCE(k.cleared, 0)           AS cleared,
        SUM(COALESCE(r.raised, 0) - COALESCE(k.cleared, 0))
            OVER (ORDER BY c.mth ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                                         AS running_backlog
FROM        cal c
LEFT JOIN   raised  r ON r.mth = c.mth
LEFT JOIN   cleared k ON k.mth = c.mth
ORDER BY c.mth;


-- @query:   Q19
-- @control: -
-- @type:    REPORT
-- @name:    Escalation list - open exceptions past SLA, highest value first
SELECT  exception_id, control_id, exception_type, severity, entity, vendor_id,
        exception_amount_usd, age_days, sla_days,
        age_days - sla_days AS days_past_sla,
        assigned_queue, exception_status, evidence
FROM    exceptions
WHERE   exception_status <> 'Resolved'
  AND   age_days > sla_days
ORDER BY exception_amount_usd DESC
LIMIT 25;
