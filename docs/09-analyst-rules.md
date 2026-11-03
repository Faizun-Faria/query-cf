# Analyst rules (phase 4)

How the AI analyst reads a question, decides what to run and writes the answer. These rules become the analyst's instructions in phase 4, and the [evaluation set](../evals/questions.csv) tests them in phase 5. A rule that code can enforce is enforced in code (see [Enforced in code](#enforced-in-code)), so the instructions carry only the judgement calls.

## What the analyst works with

Every conversation starts with the [metric catalog](08-metric-catalog.md): 8 metrics, 16 dimensions, 3 named filters, 23 company names, 9 warnings and the glossary. The analyst has three tools.

| Tool | What it does | When |
| --- | --- | --- |
| `query_metrics` | Turns metrics, groupings, filters, named filters, a date range and an optional top N into SQL through the semantic layer, runs it, and returns the rows, the SQL, the row count and the warnings of every definition used | First choice for every question (D-025) |
| `list_values` | Lists the exact values of one dimension that contain a piece of text, with their complaint counts, such as company names containing "fargo" | To turn a name the catalog doesn't list into an exact published value |
| `run_sql` | Runs one read-only query on the four mart tables after a sqlglot check. Takes the SQL, the reason no metric query can answer the question, and the metrics it follows | Only when `query_metrics` can't express the question (D-030) |

## Four kinds of answer

| Kind | When | What the analyst does | Questions |
| --- | --- | --- | --- |
| Metric | A defined metric answers the question | Runs one or more metric queries and answers | Q01, Q02, Q06-Q10, Q13-Q25 |
| SQL | The definitions exist, but a metric query can't express the shape of the question | Runs guarded SQL that follows the metric definitions, and says why | Q03, Q12 |
| Clarify | The question could mean different things that give different answers | Asks one question and runs nothing | Q04 |
| Cannot answer | The data doesn't hold what the question needs | Says what is missing, offers the closest defined metrics and runs nothing | Q05, Q11 |

## Rules

### Choosing what to run

- **R-01.** Use `query_metrics` whenever a defined metric answers the question. A question with several steps gets several metric queries, each using the previous result (Q09).
- **R-02.** Use `run_sql` only when no metric query can express the question: a minimum number of complaints per group, the lowest values first, a count of companies or groups that pass a test, or a ranking inside each group (Q03, Q12).
- **R-03.** SQL follows the catalog. A rate copies its metric's numerator and denominator exactly, so relief rates count only closed complaints with a known outcome. SQL never defines a metric in a new way.
- **R-04.** Nothing runs before a clarifying question is answered, or for a question that can't be answered.

### Reading the question

- **R-05.** Every time-based metric uses the date received. "In 2024" means received from 1 January to 31 December 2024.
- **R-06.** Relative periods count back from 31 August 2026, the end of the archive, not from today: "last year" is 2025, "this year" is 2026 to 31 August, and "the past 12 months" is 1 September 2025 to 31 August 2026. The answer gives the dates used (D-031).
- **R-07.** A question with no period gets a clarifying question (US-09), unless it says "ever", "all time" or "across the whole archive", which mean 1 December 2011 to 31 August 2026.
- **R-08.** "Credit card" means the Credit card product group, which leaves out prepaid cards (D-026). A trend that crosses April 2017 or August 2023 uses the product group, never the published product name. "Types of credit card" means the sub-product.
- **R-09.** A short name in the company list maps to its exact published name: "Citi" is CITIBANK, N.A. For any other name, `list_values` finds the exact published names. One clear match is used and named in the answer, several matches are a clarifying question, and no match is said plainly. SQL never matches part of a name (D-027).
- **R-10.** Plain words map to defined metrics and values:

  | The question says | The analyst uses |
  | --- | --- |
  | "got money back" | `monetary_relief_rate` |
  | "got relief", "got something fixed" | `relief_rate` |
  | "how often companies answered on time" | `timely_response_rate` |
  | "answered late" | `untimely_responses` |
  | "still open", "in progress" | `open_complaints` |
  | "had the consumer's own description published" | `narrative_share` |
  | "how many companies" | `companies` |
  | A state's name, such as Texas | Its postal code, TX |
  | "by phone" | `submitted_via` = Phone |
  | "servicemembers", "older Americans" | The `servicemember` and `older_american` tags |

### When to ask

- **R-11.** Ask exactly one question, before running anything, when different readings give different answers: a vague measure ("worst", "best", "performing badly"), no period (R-07), or a company name with several matches (R-09). Everything unclear goes into that one question, with the defined options to choose from listed under it, and the reply asks nothing else: no second question, however short (Q04).
- **R-12.** Don't ask when one reading is clearly meant: a company that "got" complaints received them, and "credit card complaints" means the Credit card group.

### When the question can't be answered

- **R-13.** Say what the data doesn't hold, offer the closest defined metrics, and run nothing (US-08).

  | Asked for | Why not | Offer instead |
  | --- | --- | --- |
  | Amounts of money, such as relief paid or fees charged (Q05) | The data records whether relief was monetary, not how much | `monetary_relief_rate`, or complaints closed with monetary relief |
  | Time to resolve a complaint (Q11) | There is no resolution date; the date sent to the company is when the CFPB forwarded it (D-029) | `timely_response_rate` |
  | Whether complaints were true or justified | Complaints are unverified consumer reports | What was reported |
  | Why something changed | The data records what was reported, not why | Which issues or companies changed |
  | Forecasts | The archive ends on 31 August 2026 | The past trend |
  | What consumers wrote | The analyst can't read narratives in phase 4 (D-033) | `narrative_share`, or the issues chosen |
  | Complaints per customer, or market share | There are no customer or account counts | Complaint counts |
  | Which company to choose, or other advice | QueryCF reports complaint data, not recommendations | The figures asked for, without a recommendation |

- **R-14.** A question outside the MVP scope is answered under the same rules (Q06, Q07, Q21, Q24). The scope sets what the evaluation concentrates on, not what the analyst may answer.

### Writing the answer

- **R-15.** Answer first: one sentence with the figures, then a short table when there are several rows.
- **R-16.** Every number comes from a tool result. A derived figure, such as a change between years or a share of a total, is computed in the query, not by the model.
- **R-17.** Counts are exact, with thousands separators (1,234). Rates and shares have one decimal place and a % sign (12.3%).
- **R-18.** A ranking is listed in order with its values and keeps every row tied at the cut-off, naming each tied row however many there are, and saying so (D-034, Q12). It never chooses among tied rows by another measure, such as complaint volume. A ranking by count notes that large companies rank high partly through volume (Q10).
- **R-19.** A ranking by rate leaves out companies or groups with fewer than 100 complaints in the period, unless the question sets its own minimum, and says which minimum it used (D-032).
- **R-20.** After the figures, say how they were worked out: the metric, the period, every filter and named filter, and the exact company names. An answer from SQL also says why no metric query could answer it.
- **R-21.** Show every warning the tools return, one line each, keeping its meaning (Q06, Q15, Q22, Q24, Q25).
- **R-22.** Describe reports, not findings: "consumers reported" or "complaints about", never "Wells Fargo opened accounts". Avoid judgement words such as "worst" or "significant" unless the question defined them.
- **R-23.** Describe a trend by its direction and its first, last and highest values, and flag partial years and renamed issues.

### Follow-up questions

- **R-24.** A follow-up keeps the previous question's period and filters unless it changes them ("same for Citi"), and the answer says what it kept.

## Enforced in code

| Rule | How |
| --- | --- |
| Only read-only SQL on `marts.fct_complaints`, `marts.dim_company`, `marts.dim_product` and `marts.dim_date` | `run_sql` parses every query with sqlglot first; anything else is blocked, not run, and logged (D-006, US-10). Every query also runs on a read-only connection that can't touch files (D-038) |
| Narrative text can't be read | `marts.complaint_narratives` is not on the allowlist (D-033) |
| `run_sql` explains itself | A call without a reason, or naming an unknown metric, is refused. It returns the warnings of the metrics it follows, plus `reports_not_findings` |
| Warnings travel with answers | `query_metrics` returns the warnings of every definition it used |
| Ties at the cut-off are visible | `query_metrics` with a top N reports when the next row ties with the last one kept |
| Answers show their working | The SQL and row count shown with an answer come from the tool results, not from the model's text (US-07) |
| Results stay small | `run_sql` returns at most 200 rows, `list_values` at most 25 values, and any query is stopped after 30 seconds |
| Every question is logged | The question, every tool call as the plan, the SQL, row counts, the answer, the model version and a timestamp (US-11); see the [AI analyst design](10-ai-analyst.md) |

## How the evaluation set tests the rules

| Rules | Questions |
| --- | --- |
| R-01: metric queries first, in several steps when needed | Q01, Q09, Q49 |
| R-02, R-03: guarded SQL that follows the definitions | Q03, Q12, Q37, Q38 |
| R-06: relative periods, counted back from 31 August 2026 | Q26, Q27, Q28 |
| R-07, R-12: a missing period, and a period given in words | Q29, Q30, Q31 |
| R-08: product groups across renames, and sub-products | Q07, Q08, Q21 |
| R-09: short company names, and names outside the list | Q01, Q13, Q24, Q32, Q33 |
| R-10: plain words for metrics and values | Q14, Q15, Q16, Q17, Q18, Q19, Q20, Q45 |
| R-11: one clarifying question | Q04, Q29, Q30, Q33, Q50 |
| R-13: questions that can't be answered | Q05, Q11, Q42, Q43 |
| R-14: questions outside the MVP scope | Q06, Q07, Q21, Q24, Q31, Q32, Q46, Q48 |
| R-16: derived figures computed in a query | Q34, Q35, Q36 |
| R-18: ties and rankings by count | Q10, Q12, Q44 |
| R-19: the default minimum for rate rankings | Q37, Q38 |
| R-21: warnings | Q06, Q15, Q22, Q24, Q25, Q46 |
| R-24: follow-up questions | Q39, Q40, Q41 |

Every rule is now tested by at least one question. The rules about wording (R-15, R-17, R-20, R-22, R-23) aren't scored, as the [evaluation design](11-evaluation.md) explains; the question log keeps what's needed to check them.
