# Evaluation (phase 5)

The evaluation asks the analyst every question in [evals/questions.csv](../evals/questions.csv) and scores each answer against the answer key, which was computed in SQL before the analyst ever ran ([evals/answer_key.sql](../evals/answer_key.sql)). The latest run is in the [evaluation results](12-evaluation-results.md).

## How a run works

1. `python run_pipeline.py evaluate --runs 3` reads the questions and checks the file: eight columns, unique IDs, a known kind and difficulty, and an expected answer for every question.
2. Each question is asked three times, each time in a new conversation, so no answer leans on an earlier one (D-042). Each answer is logged like any other question, tagged with its evaluation ID and run number.
3. Each answer is scored by code (D-040), and checked for figures no tool returned (D-043), as below.
4. The results go to `docs/12-evaluation-results.md`: how many questions passed every run, the pass rate over all answers, both by difficulty and by kind, the cost and tokens of the run, and every failed answer. If the Claude API fails on any question, no report is written, so a broken run never looks like a bad score.

`--only Q04 --only Q11` re-runs some questions without rewriting the report and prints each failed answer, which is useful after changing a rule.

## Why each question is asked more than once

The model doesn't give the same answer every time. In the second run of the first 25 questions, three answers that had passed the first time failed: one wrote "Eighteen" for 18, which the scorer now reads; one asked two questions instead of one; and one listed 5 of 13 companies tied at the top. One run can't tell a question the analyst always gets right from one it gets right by luck, so the results report both "passed every run" and the pass rate over every answer. Three runs of 25 questions cost a few cents.

## How an answer is scored

| Kind | Passes when |
| --- | --- |
| `metric`, `sql` | Every expected figure is in the answer, every expected name or period is too, and at least one query ran |
| `clarify` | No tool was called, and the answer asks exactly one question, or makes one request without a question mark, such as "Please reply with one choice from each list" (US-09, D-045) |
| `cannot_answer` | No tool was called, and the answer offers a metric the expected answer names, such as the monetary relief rate (US-08) |

Figures are matched as written by the rules (R-17): 7,058 or 7058, and 33.3%, 33.3 % or 33.3 percent, but not 33.25% or "about 7,000". A count up to ninety-nine may also be written as words, such as "Eighteen". Periods are matched as years, or as 2026-01, January 2026 or Jan 2026, or as the month name alone when the year appears anywhere in the answer, as in a table headed "Month (2026)" (D-046). Names are matched on their first distinctive word, so "TransUnion" counts for TRANSUNION INTERMEDIATE HOLDINGS, INC. and "Bank of America" for BANK OF AMERICA, NATIONAL ASSOCIATION; words such as "bank" and "financial", which many companies share, are skipped. Because every tied company has its own label, a ranking that leaves out companies tied at the cut-off fails (R-18).

## Figures no tool returned

Separately from the score, every figure in every answer is checked against the tool results, the question and the system prompt (R-16). A figure found in none of them is usually the model's own arithmetic, such as a change between two years. It is listed in the results but doesn't fail the question, because the arithmetic may be right; the list shows how often the analyst strays from its sources. Rates, decimals and counts of 100 or more are checked; years, dates and small whole numbers such as ranks are not.

## What the score doesn't check

- **Which figure goes with which label.** An answer that swaps two years' rates still passes. Every failure is listed with its answer, and passes can be read in the question log.
- **Wording.** Warnings (R-21), describing reports rather than findings (R-22) and the explanation of how a figure was worked out (R-20) aren't scored. The log keeps every warning the tools returned, so these can be checked later.

An AI judge could score wording, but its scores would vary from run to run and would need checking themselves; scoring by code gives the same score for the same answer every time.

## When to run it

- Before changing the model or effort in `config/analyst.yaml` (US-12). A test fails if the committed results were produced by a different model or effort than the one pinned.
- After changing the analyst rules, the semantic layer or the tools, which change the prompt version shown in the results.

The run needs the full warehouse and an API key, so it runs on a laptop, not in CI; CI runs the tests of the scoring itself (D-041).

The rules have been adjusted once after seeing these results (D-044), so the first 25 questions have helped shape the analyst. The next 25 questions are the fairer test: they should be written and scored before the rules change again.

## Tests

- The real question bank loads, and a perfect answer to each of the 25 questions passes.
- Figures, periods and names are matched however they are written, counts written as words included, but a figure with the wrong number of decimal places, a missing row, a missing tied company or a figure with no query behind it fails.
- Clarifying and cannot-answer replies fail if they run a query, ask two questions, ask nothing or offer no defined metric; a request worded without a question mark passes.
- A figure no tool returned is flagged without failing the answer; figures from the tool results, the question and the prompt, and dates and ranks, are never flagged.
- The report counts repeated runs per question, by difficulty and by kind.
- The evaluate command runs against the planted warehouse with a fake model: it scores each question in its own conversation, tags the log with the question and run, asks every question several times, prints failed answers when no report is written, re-runs single questions without rewriting the report, and writes no report when the API fails.
