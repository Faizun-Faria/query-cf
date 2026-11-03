-- Answer key for evals/questions.csv.
--
-- Each block starts with "-- Qnn" and holds one of:
--   a SQL query, whose result is the expected answer; or
--   an "-- expect:" line, the behaviour expected when there is no number to give.
--
-- A metric question also has a "-- query:" line: the same question as arguments to
-- `run_pipeline.py query`, whose answer must match the SQL's, so a metric answer can be
-- computed twice, independently. "-- no query:" says why a metric question can't be checked
-- that way. A follow-up question has a "-- follows:" line naming the question it continues;
-- its SQL answers the follow-up on its own. A "-- check:" query only confirms an assumption,
-- such as how many companies a name matches; it isn't the answer.
--
-- Columns ending in _rate or _share are percentages, rounded to one decimal place only when
-- the expected answer is written, never in the SQL. A result with several columns is written
-- as label=value pairs, in the order the query returns them.

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

-- Q26
-- query: timely_response_rate --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
-- "Last year" counts back from the end of the archive, 31 August 2026, so it means 2025.
SELECT 100.0 * COUNT(DISTINCT complaint_id) FILTER (WHERE is_timely_response)
         / COUNT(DISTINCT complaint_id) AS timely_response_rate
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q27
-- query: complaints --where product_group="Credit card" --from 2025-09-01 --to 2026-08-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE date_received BETWEEN DATE '2025-09-01' AND DATE '2026-08-31'
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q28
-- query: complaints --where company="Capital One" --where product_group="Credit card" --from 2026-01-01 --to 2026-08-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2026
  AND company_key IN (
    SELECT company_key FROM marts.dim_company
    WHERE company_name = 'CAPITAL ONE FINANCIAL CORPORATION'
  )
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q29
-- expect: CLARIFY: asks one question about which period to count (and whether to keep to one product), before running any SQL

-- Q30
-- expect: CLARIFY: asks one question about which period the relief rate should cover, before running any SQL

-- Q31
-- query: complaints --where company="Discover"
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE company_key IN (
    SELECT company_key FROM marts.dim_company
    WHERE company_name = 'DISCOVER BANK'
  );

-- Q32
-- check: companies whose name contains "ocwen"; the question assumes there is exactly one
SELECT c.company_name, COUNT(DISTINCT f.complaint_id) AS total_complaints
FROM marts.fct_complaints AS f
JOIN marts.dim_company AS c ON c.company_key = f.company_key
WHERE lower(c.company_name) LIKE '%ocwen%'
GROUP BY c.company_name;

-- query: complaints --where company="Ocwen Financial Corporation" --where product_group=Mortgage --from 2016-01-01 --to 2016-12-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2016
  AND company_key IN (
    SELECT company_key FROM marts.dim_company
    WHERE company_name = 'Ocwen Financial Corporation'
  )
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Mortgage'
  );

-- Q33
-- expect: CLARIFY: asks one question about which company "Citizens" means, naming the matches such as CITIZENS FINANCIAL GROUP, INC. and FIRST CITIZENS BANCSHARES, INC., before running any SQL
-- check: companies whose name contains "citizens", with their 2025 credit card complaints
SELECT c.company_name,
       COUNT(DISTINCT f.complaint_id) AS all_complaints,
       COUNT(DISTINCT f.complaint_id) FILTER (
         WHERE YEAR(f.date_received) = 2025 AND p.product_group = 'Credit card'
       ) AS credit_card_2025
FROM marts.fct_complaints AS f
JOIN marts.dim_company AS c ON c.company_key = f.company_key
JOIN marts.dim_product AS p ON p.product_key = f.product_key
WHERE lower(c.company_name) LIKE '%citizens%'
GROUP BY c.company_name
ORDER BY all_complaints DESC;

-- Q34
-- no query: a change between two years is a derived figure, which no metric computes
SELECT 100.0 * (
         COUNT(DISTINCT complaint_id) FILTER (WHERE YEAR(date_received) = 2025)
         - COUNT(DISTINCT complaint_id) FILTER (WHERE YEAR(date_received) = 2024)
       ) / COUNT(DISTINCT complaint_id) FILTER (WHERE YEAR(date_received) = 2024) AS change_rate
FROM marts.fct_complaints
WHERE YEAR(date_received) IN (2024, 2025)
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q35
-- no query: a share of all complaints is a derived figure, which no metric computes
SELECT 100.0 * COUNT(DISTINCT f.complaint_id) FILTER (WHERE p.product_group = 'Credit card')
         / COUNT(DISTINCT f.complaint_id) AS credit_card_share
FROM marts.fct_complaints AS f
JOIN marts.dim_product AS p ON p.product_key = f.product_key
WHERE YEAR(f.date_received) = 2025;

-- Q36
-- no query: a difference between two companies is a derived figure, which no metric computes
SELECT COUNT(DISTINCT f.complaint_id) FILTER (
         WHERE c.company_name = 'CAPITAL ONE FINANCIAL CORPORATION'
       )
       - COUNT(DISTINCT f.complaint_id) FILTER (WHERE c.company_name = 'CITIBANK, N.A.')
         AS difference
FROM marts.fct_complaints AS f
JOIN marts.dim_company AS c ON c.company_key = f.company_key
JOIN marts.dim_product AS p ON p.product_key = f.product_key
WHERE YEAR(f.date_received) = 2025
  AND p.product_group = 'Credit card';

-- Q37
-- No minimum is given, so companies with fewer than 100 complaints are left out (R-19, D-032).
-- Ties at the cut-off are all kept, so the list can hold more than 5 companies.
WITH rates AS (
  SELECT c.company_name,
         100.0 * COUNT(DISTINCT f.complaint_id) FILTER (WHERE f.is_timely_response)
           / COUNT(DISTINCT f.complaint_id) AS timely_response_rate
  FROM marts.fct_complaints AS f
  JOIN marts.dim_company AS c ON c.company_key = f.company_key
  JOIN marts.dim_product AS p ON p.product_key = f.product_key
  WHERE YEAR(f.date_received) = 2025
    AND p.product_group = 'Credit card'
  GROUP BY c.company_name
  HAVING COUNT(DISTINCT f.complaint_id) >= 100
)
SELECT company_name, timely_response_rate
FROM (SELECT *, RANK() OVER (ORDER BY timely_response_rate) AS place FROM rates)
WHERE place <= 5
ORDER BY place, company_name;

-- Q38
-- No minimum is given, so companies with fewer than 100 complaints are left out (R-19, D-032).
-- The relief rate counts closed complaints with a known outcome only, as its metric does.
WITH rates AS (
  SELECT c.company_name,
         100.0 * COUNT(DISTINCT f.complaint_id) FILTER (
           WHERE f.response_status = 'closed'
             AND f.relief_type IN ('monetary', 'non-monetary', 'type not recorded')
         ) / NULLIF(COUNT(DISTINCT f.complaint_id) FILTER (
           WHERE f.response_status = 'closed' AND f.relief_type <> 'unknown'
         ), 0) AS relief_rate
  FROM marts.fct_complaints AS f
  JOIN marts.dim_company AS c ON c.company_key = f.company_key
  JOIN marts.dim_product AS p ON p.product_key = f.product_key
  WHERE YEAR(f.date_received) = 2024
    AND p.product_group = 'Credit card'
  GROUP BY c.company_name
  HAVING COUNT(DISTINCT f.complaint_id) >= 100
)
SELECT company_name, relief_rate
FROM (SELECT *, RANK() OVER (ORDER BY relief_rate DESC NULLS LAST) AS place FROM rates)
WHERE place <= 5
ORDER BY place, company_name;

-- Q39
-- follows: Q13
-- query: complaints --where company="Capital One" --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND company_key IN (
    SELECT company_key FROM marts.dim_company
    WHERE company_name = 'CAPITAL ONE FINANCIAL CORPORATION'
  )
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q40
-- follows: Q02
-- query: monetary_relief_rate --by received:year --where product_group="Credit card" --from 2022-01-01 --to 2025-12-31
SELECT YEAR(date_received) AS year,
       100.0 * COUNT(DISTINCT complaint_id) FILTER (WHERE relief_type = 'monetary')
         / COUNT(DISTINCT complaint_id) AS monetary_relief_rate
FROM marts.fct_complaints
WHERE YEAR(date_received) BETWEEN 2022 AND 2025
  AND response_status = 'closed' AND relief_type <> 'unknown'  -- closed, with a known outcome
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
GROUP BY YEAR(date_received)
ORDER BY year;

-- Q41
-- follows: Q17
-- query: complaints --where state=FL --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
SELECT COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND state = 'FL'
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  );

-- Q42
-- expect: CANNOT: the analyst can't read the consumers' own descriptions in phase 4; offers the share with narrative or the issues consumers chose instead

-- Q43
-- expect: CANNOT: the data records what was reported, not why it changed; offers the relief rate by issue or by company instead

-- Q44
-- query: complaints --by state --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31 --top 5
-- Ties at the cut-off are all kept.
SELECT state, total_complaints
FROM (
  SELECT state, COUNT(DISTINCT complaint_id) AS total_complaints,
         RANK() OVER (ORDER BY COUNT(DISTINCT complaint_id) DESC) AS place
  FROM marts.fct_complaints
  WHERE YEAR(date_received) = 2025
    AND product_key IN (
      SELECT product_key FROM marts.dim_product
      WHERE product_group = 'Credit card'
    )
  GROUP BY state
)
WHERE place <= 5
ORDER BY place, state;

-- Q45
-- query: complaints --by received:quarter --where older_american=true --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
SELECT concat(YEAR(date_received), '-Q', QUARTER(date_received)) AS received_quarter,
       COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND is_older_american
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
GROUP BY received_quarter
ORDER BY received_quarter;

-- Q46
-- query: complaints --by received:year --where company="Wells Fargo" --where product_group="Bank account" --segment accounts_opened_without_consent --from 2018-01-01 --to 2025-12-31
-- The bank account sub-issue was renamed in August 2023, so both names are needed.
SELECT YEAR(date_received) AS year, COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) BETWEEN 2018 AND 2025
  AND company_key IN (
    SELECT company_key FROM marts.dim_company
    WHERE company_name = 'WELLS FARGO & COMPANY'
  )
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Bank account'
  )
  AND (
    issue = 'Unsolicited issuance of credit card'
    OR sub_issue IN (
      'Account opened as a result of fraud',
      'Account opened without my consent or knowledge',
      'Card opened as result of identity theft or fraud',
      'Card opened without my consent or knowledge'
    )
  )
GROUP BY YEAR(date_received)
ORDER BY year;

-- Q47
-- query: complaints --by company_response --where product_group="Credit card" --from 2025-01-01 --to 2025-12-31
SELECT company_response, COUNT(DISTINCT complaint_id) AS total_complaints
FROM marts.fct_complaints
WHERE YEAR(date_received) = 2025
  AND product_key IN (
    SELECT product_key FROM marts.dim_product
    WHERE product_group = 'Credit card'
  )
GROUP BY company_response
ORDER BY total_complaints DESC, company_response;

-- Q48
-- query: complaints --by issue --where product_group="Debt collection" --from 2025-01-01 --to 2025-12-31 --top 5
-- Ties at the cut-off are all kept.
SELECT issue, total_complaints
FROM (
  SELECT issue, COUNT(DISTINCT complaint_id) AS total_complaints,
         RANK() OVER (ORDER BY COUNT(DISTINCT complaint_id) DESC) AS place
  FROM marts.fct_complaints
  WHERE YEAR(date_received) = 2025
    AND product_key IN (
      SELECT product_key FROM marts.dim_product
      WHERE product_group = 'Debt collection'
    )
  GROUP BY issue
)
WHERE place <= 5
ORDER BY place, issue;

-- Q49
-- no query: two steps, and the second depends on the answer to the first
-- LIMIT 4 is one more than asked, to show whether third place is tied.
WITH cards AS (
  SELECT f.complaint_id, f.state, c.company_name
  FROM marts.fct_complaints AS f
  JOIN marts.dim_company AS c ON c.company_key = f.company_key
  JOIN marts.dim_product AS p ON p.product_key = f.product_key
  WHERE YEAR(f.date_received) = 2025
    AND p.product_group = 'Credit card'
),
top_state AS (
  SELECT state
  FROM cards
  GROUP BY state
  ORDER BY COUNT(DISTINCT complaint_id) DESC, state
  LIMIT 1
)
SELECT state, company_name, COUNT(DISTINCT complaint_id) AS total_complaints
FROM cards
WHERE state = (SELECT state FROM top_state)
GROUP BY state, company_name
ORDER BY total_complaints DESC, company_name
LIMIT 4;

-- Q50
-- expect: CLARIFY: asks one question about what "best" means (the fewest complaints, the highest timely response rate or the highest relief rate) and for which period, before running any SQL
