# Chat app (phase 6)

A Streamlit chat app over the same analyst as `ask` and `chat`: a visitor asks in plain English and gets the answer in the format that fits it, with every query behind it (US-07, US-13). Each visitor has their own conversation, so follow-up questions work (R-24), and every question goes to the question log tagged `source: app` (US-11).

```bash
streamlit run app/streamlit_app.py
```

## How an answer is shown

Code chooses the format from the shape of the query results, not the model, so the same answer always looks the same and the choice can be tested (D-051).

| The queries that ran | The app shows |
| --- | --- |
| None: a clarifying question, or one the data can't answer | The text only |
| One, with one row and one figure | The text, then the figure as a large number |
| One, grouped by date received | The text, then a line chart |
| One, grouped by a category such as company, state or issue | The text, then a bar chart with the largest at the top |
| Several | A short report: the text, then each query's number or chart |

Charts are drawn from raw values that the tools return next to the formatted rows. The model never sees the raw values, only the formatted rows, so the analyst's instructions and its evaluation results are unchanged. Rates are charted as percentages, and a year from SQL labels the rows rather than being charted as a figure.

Under every answer, "How this was worked out" lists each tool call, its SQL, its warnings, and the tokens and cost of the answer.

## The demo's data

The app reads the full warehouse when there is one, as on a laptop. Streamlit Community Cloud has no warehouse and at most about 2.7 GB of memory, so the public demo reads a copy of only the four mart tables the analyst may query (D-052):

```bash
python run_pipeline.py demo-warehouse   # writes data/demo/query_cf_demo.duckdb and prints its SHA-256
```

The file is uploaded as a GitHub release asset, and its URL and SHA-256 go in `config/app.yaml`. On first start the app downloads it, checks the SHA-256 and only then opens it; a file that doesn't match is deleted. The copy holds no narratives and no raw data, so the demo couldn't show narrative text even if a query asked for it. DuckDB's memory is capped at 1 GB, and the connection is read-only and locked, as for `ask`.

## Keeping the demo cheap

| Limit | How it works |
| --- | --- |
| Questions per visit | 10, counted for each browser session. A new visit starts again, so this only slows a single visitor down |
| Length | 500 characters per question |
| A day's spending | $1.00 a day across all visitors, added up from the question log's `cost_usd` for questions tagged `source: app` since midnight UTC. If the log can't be read, the demo stops answering rather than guess |
| The bill | The demo runs on its own API key, with a monthly spend limit set in the Anthropic Console. Whatever else fails, the bill can't pass it |

At the measured $0.08 per 100 questions, $1 a day is about 1,200 questions (D-053). The limits are in `config/app.yaml`.

Streamlit Community Cloud puts an app to sleep after 12 hours without visitors; the next visitor wakes it, which takes a minute while the data downloads again.

## Secrets

The app reads `ANTHROPIC_API_KEY` and `QUERY_CF_MONGODB_URI` from `.env` locally, and from the app's secrets on Streamlit Community Cloud. Neither is ever in the repository: `.env` and `.streamlit/secrets.toml` are gitignored.

## Tests

- The tools return raw values for charts, and the model's tool results leave them out.
- Each format rule is checked on the planted warehouse: a number, a line chart labelled 2016 to 2025, a bar chart with the largest at the top, percentages, a year from SQL, a report, and text when no query ran.
- Every limit refuses what it should; spending is counted only for the app's questions since midnight, adds what fell back to the file, and stops the demo when it can't be counted.
- The demo data is downloaded once, checked, and deleted when its SHA-256 doesn't match; `demo-warehouse` copies the four mart tables and nothing else.
- The page itself runs under Streamlit's own test runner with a fake model: a number with its SQL shown, a chart, an example question clicked, the question limit, and a missing API key.
