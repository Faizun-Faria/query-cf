# Evaluation (phase 5)

The evaluation asks the analyst every question in [evals/questions.csv](../evals/questions.csv) and scores each answer against the answer key, which was computed in SQL before the analyst ever ran ([evals/answer_key.sql](../evals/answer_key.sql)). The latest run is in the [evaluation results](12-evaluation-results.md).

## How a run works

1. `python run_pipeline.py evaluate` reads the questions and checks the file: eight columns, unique IDs, a known kind and difficulty, and an expected answer for every question.
2. Each question is asked in a new conversation, so no answer leans on an earlier one. Each is logged like any other question, tagged with its evaluation ID.
3. Each answer is scored by code (D-040), as below.
4. The results go to `docs/12-evaluation-results.md`: accuracy overall, by difficulty and by kind, the cost and tokens of the run, and every failure with the analyst's answer. If the Claude API fails on any question, no report is written, so a broken run never looks like a bad score.

`--only Q04 --only Q11` re-runs some questions without rewriting the report, which is useful after changing a rule.

## How an answer is scored

| Kind | Passes when |
| --- | --- |
| `metric`, `sql` | Every expected figure is in the answer, every expected name or period is too, and at least one query ran |
| `clarify` | No tool was called, and the answer asks exactly one question (US-09) |
| `cannot_answer` | No tool was called, and the answer offers a metric the expected answer names, such as the monetary relief rate (US-08) |

Figures are matched as written by the rules (R-17): 7,058 or 7058, and 33.3%, 33.3 % or 33.3 percent, but not 33.25% or "about 7,000". Periods are matched as years, or as 2026-01, January 2026 or Jan 2026. Names are matched on their first distinctive word, so "TransUnion" counts for TRANSUNION INTERMEDIATE HOLDINGS, INC. and "Bank of America" for BANK OF AMERICA, NATIONAL ASSOCIATION; words such as "bank" and "financial", which many companies share, are skipped. Because every tied company has its own label, a ranking that leaves out companies tied at the cut-off fails (R-18).

## What the score doesn't check

- **Which figure goes with which label.** An answer that swaps two years' rates still passes. Every failure is listed with its answer, and passes can be read in the question log.
- **Wording.** Warnings (R-21), describing reports rather than findings (R-22) and the explanation of how a figure was worked out (R-20) aren't scored. The log keeps every warning the tools returned, so these can be checked later.
- **More than one run.** The model can answer the same question differently on another run; a question that fails can be re-run with `--only`.

An AI judge could score wording, but its scores would vary from run to run and would need checking themselves; scoring by code gives the same score for the same answer every time.

## When to run it

- Before changing the model or effort in `config/analyst.yaml` (US-12). A test fails if the committed results were produced by a different model or effort than the one pinned.
- After changing the analyst rules, the semantic layer or the tools, which change the prompt version shown in the results.

The run needs the full warehouse and an API key, so it runs on a laptop, not in CI; CI runs the tests of the scoring itself (D-041).

## Tests

- The real question bank loads, and a perfect answer to each of the 25 questions passes.
- Figures, periods and names are matched however they are written, but a figure with the wrong number of decimal places, a missing row, a missing tied company or a figure with no query behind it fails.
- Clarifying and cannot-answer replies fail if they run a query, ask two questions or offer no defined metric.
- The evaluate command runs against the planted warehouse with a fake model: it scores each question in its own conversation, tags the log, re-runs single questions without rewriting the report, and writes no report when the API fails.
