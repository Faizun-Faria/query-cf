-- Answer key for evals/questions.csv.
--
-- Each block starts with "-- Qnn" and holds one of:
--   a SQL query, whose result is the expected answer; or
--   an "-- expect:" line, the behaviour expected when there is no number to give.
--
-- A metric question also has a "-- query:" line: the same question as arguments to
-- `run_pipeline.py query`. `run_pipeline.py answer-key` runs both and stops if they disagree,
-- so every metric answer is computed twice, independently. "-- no query:" says why a metric
-- question can't be checked that way.
--
-- Columns ending in _rate or _share are percentages. A result with several columns is
-- written as label=value pairs, in the order the query returns them.

-- Q01
-- query: complaints --where company="Capital One" --where product_group="Credit card" --from 2024-01-01 --to 2024-12-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2024
  AND company_key IN (
    SELECT company_key FROM marts.dim_company
    WHERE company_name = 'CAPITAL ONE FINANCIAL CORPORATION'
  )
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q02
-- query: relief_rate --by received:year --where product_group="Credit card" --from 2022-01-01 --to 2025-12-31
SELECT YEAR(date_received) AS year,
       100.0 * COUNT(DISTINCT complaint_id) FILTER (
         WHERE relief_type IN ('monetary', 'non-monetary', 'type not recorded')
       ) / COUNT(DISTINCT complaint_id) AS relief_rate
FROM marts.fct_complaints
WHERE YEAR(date_received) BETWEEN 2022 AND 2025
  AND response_status = 'closed' AND relief_type <> 'unknown'  -- closed, with a known outcome
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
GROUP BY YEAR(date_received)
ORDER BY year;

-- Q03
SELECT COUNT(*) AS companies
FROM (
  SELECT company_key
  FROM marts.fct_complaints
  WHERE YEAR(date_received) = 2025
    AND product_key IN (
      SELECT product_key FROM marts.dim_product
      WHERE product_group = 'Credit card'
    )
  GROUP BY company_key
  HAVING COUNT(DISTINCT complaint_id) >= 1000
);

-- Q04
-- expect: CLARIFY: asks one question about what "worst" means (most complaints, the lowest timely response rate or the lowest relief rate) and for which period and product, before running any SQL

-- Q05
-- expect: CANNOT: the data records whether relief was monetary, not how much was paid; offers the monetary relief rate or the number of complaints closed with monetary relief instead

-- Q06
-- query: complaints --by company --top 10
SELECT c.company_name, COUNT(DISTINCT f.complaint_id) AS total_complaints
FROM marts.fct_complaints AS f
JOIN marts.dim_company AS c ON c.company_key = f.company_key
GROUP BY c.company_name
ORDER BY total_complaints DESC, c.company_name
LIMIT 10;

-- Q07
-- query: complaints --by product_group --from 2025-01-01 --to 2025-12-31 --top 10
SELECT p.product_group, COUNT(DISTINCT f.complaint_id) AS total_complaints
FROM marts.fct_complaints AS f
JOIN marts.dim_product AS p ON p.product_key = f.product_key
WHERE YEAR(f.date_received) = 2025
GROUP BY p.product_group
ORDER BY total_complaints DESC, p.product_group
LIMIT 10;

-- Q08
-- query: complaints --by sub_product --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
SELECT p.sub_product, COUNT(DISTINCT f.complaint_id) AS total_complaints
FROM marts.fct_complaints AS f
JOIN marts.dim_product AS p ON p.product_key = f.product_key
WHERE YEAR(f.date_received) = 2025
  AND p.product_group = 'Credit card'
GROUP BY p.sub_product
ORDER BY total_complaints DESC, p.sub_product;

-- Q09
-- no query: two steps, and the second depends on the answer to the first
WITH march AS (
  SELECT f.complaint_id, p.product_group, c.company_name
  FROM marts.fct_complaints AS f
  JOIN marts.dim_product AS p ON p.product_key = f.product_key
  JOIN marts.dim_company AS c ON c.company_key = f.company_key
  WHERE f.date_received BETWEEN DATE '2025-03-01' AND DATE '2025-03-31'
),
top_group AS (
  SELECT product_group
  FROM march
  GROUP BY product_group
  ORDER BY COUNT(DISTINCT complaint_id) DESC, product_group
  LIMIT 1
)
SELECT product_group, company_name, COUNT(DISTINCT complaint_id) AS total_complaints
FROM march
WHERE product_group = (SELECT product_group FROM top_group)
GROUP BY product_group, company_name
ORDER BY total_complaints DESC, company_name
LIMIT 5;

-- Q10
-- query: untimely_responses --by company --segment mvp_scope --top 10
SELECT c.company_name, COUNT(DISTINCT f.complaint_id) AS untimely_responses
FROM marts.fct_complaints AS f
JOIN marts.dim_company AS c ON c.company_key = f.company_key
WHERE f.response_status = 'untimely'
  AND f.date_received BETWEEN DATE '2021-09-01' AND DATE '2026-08-31'
  AND f.product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
GROUP BY c.company_name
ORDER BY untimely_responses DESC, c.company_name
LIMIT 10;

-- Q11
-- expect: CANNOT: the data has no date when a complaint was resolved, only whether the company responded on time, and the date sent to the company is not a resolution; offers the timely response rate instead

-- Q12
-- Ties at the cut-off are all kept, so either list can hold more than 5 companies.
WITH rates AS (
  SELECT c.company_name,
         100.0 * COUNT(DISTINCT f.complaint_id) FILTER (WHERE f.is_timely_response)
           / COUNT(DISTINCT f.complaint_id) AS timely_response_rate
  FROM marts.fct_complaints AS f
  JOIN marts.dim_company AS c ON c.company_key = f.company_key
  WHERE YEAR(f.date_received) = 2025
    AND f.product_key IN (
      SELECT product_key FROM marts.dim_product
      WHERE product_group = 'Credit card'
    )
  GROUP BY c.company_name
  HAVING COUNT(DISTINCT f.complaint_id) >= 1000
),
ranked AS (
  SELECT *,
         RANK() OVER (ORDER BY timely_response_rate DESC) AS from_top,
         RANK() OVER (ORDER BY timely_response_rate ASC) AS from_bottom
  FROM rates
)
SELECT ranking, company_name, timely_response_rate
FROM (
  SELECT 'highest' AS ranking, company_name, timely_response_rate, from_top AS place
  FROM ranked WHERE from_top <= 5
  UNION ALL
  SELECT 'lowest', company_name, timely_response_rate, from_bottom
  FROM ranked WHERE from_bottom <= 5
)
ORDER BY ranking, place, company_name;

-- Q13
-- query: complaints --where company="Citi" --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND company_key IN (
    SELECT company_key FROM marts.dim_company
    WHERE company_name = 'CITIBANK, N.A.'
  )
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q14
-- query: monetary_relief_rate --where product_group="Credit card" --from 2024-01-01 --to 2024-12-31
SELECT 100.0 * COUNT(DISTINCT complaint_id) FILTER (WHERE relief_type = 'monetary')
         / NULLIF(COUNT(DISTINCT complaint_id), 0) AS monetary_relief_rate
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2024
  AND response_status = 'closed' AND relief_type <> 'unknown'  -- closed, with a known outcome
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q15
-- query: open_complaints --where product_group="Credit card" --from 2026-01-01 --to 2026-08-31
SELECT COUNT(DISTINCT complaint_id) AS open_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2026
  AND response_status = 'in progress'
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q16
-- query: companies --segment mvp_scope
SELECT COUNT(DISTINCT company_key) AS companies
FROM marts.fct_complaints
WHERE date_received BETWEEN DATE '2021-09-01' AND DATE '2026-08-31'
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q17
-- query: complaints --where state=TX --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND state = 'TX'
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q18
-- query: narrative_share --where product_group="Credit card" --from 2023-01-01 --to 2023-12-31
SELECT 100.0 * COUNT(DISTINCT complaint_id) FILTER (WHERE has_narrative)
         / NULLIF(COUNT(DISTINCT complaint_id), 0) AS narrative_share
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2023
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q19
-- query: complaints --where submitted_via=Phone --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND submitted_via = 'Phone'
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q20
-- query: complaints --where servicemember=true --where product_group="Credit card" --from 2024-01-01 --to 2024-12-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2024
  AND is_servicemember
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q21
-- query: complaints --by received:year --where product_group="Credit card" --from 2015-01-01 --to 2019-12-31
SELECT YEAR(date_received) AS year, COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) BETWEEN 2015 AND 2019
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
GROUP BY YEAR(date_received)
ORDER BY year;

-- Q22
-- query: complaints --by received:month --where product_group="Credit card" --from 2026-01-01 --to 2026-12-31
SELECT strftime(date_received, '%Y-%m') AS month, COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2026
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
GROUP BY month
ORDER BY month;

-- Q23
-- query: complaints --by issue --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31 --top 5
SELECT issue, COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
GROUP BY issue
ORDER BY total_complaints DESC, issue
LIMIT 5;

-- Q24
-- query: complaints --by received:year --where company="Wells Fargo" --segment account_opening_issues --from 2013-01-01 --to 2016-12-31
-- Before April 2017 the only matching issue also covers closing and managing an account.
SELECT YEAR(date_received) AS year, COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) BETWEEN 2013 AND 2016
  AND company_key IN (
    SELECT company_key FROM marts.dim_company
    WHERE company_name = 'WELLS FARGO & COMPANY'
  )
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Bank account'
  )
  AND issue IN ('Account opening, closing, or management', 'Opening an account')
GROUP BY YEAR(date_received)
ORDER BY year;

-- Q25
-- query: complaints --by received:year --segment accounts_opened_without_consent --where product_group="Credit card" --from 2022-01-01 --to 2025-12-31
-- The card sub-issue was renamed in August 2023, so both names are needed.
SELECT YEAR(date_received) AS year, COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) BETWEEN 2022 AND 2025
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
  AND (
    issue = 'Unsolicited issuance of credit card'
    OR sub_issue IN (
      'Card opened as result of identity theft or fraud',
      'Card opened without my consent or knowledge',
      'Account opened as a result of fraud',
      'Account opened without my consent or knowledge'
    )
  )
GROUP BY YEAR(date_received)
ORDER BY year;
