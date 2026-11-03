# AI analyst (phase 4)

The analyst turns a plain-English question into answers from the semantic layer, and shows and logs how it got there. It follows the [analyst rules](09-analyst-rules.md); the [evaluation set](../evals/questions.csv) measures how well in phase 5.

## How a question is answered

1. The question goes to Claude with the system prompt and three tools. The system prompt is the `## Rules` section of the analyst rules, a list of the tables SQL may read, and the [metric catalog](08-metric-catalog.md), all built from files in the repository (D-039).
2. Claude asks for tools. Each call is checked and run; errors and blocked queries go back to Claude as errors, so it can correct itself.
3. When Claude answers, the answer is printed with every query that ran, its SQL and its row count, then the tokens and cost.
4. The whole exchange is written to the question log as one record (US-11).

The loop is written out in `query_cf/analyst.py` rather than left to the SDK's beta tool runner, so every step is logged as it happens and nothing depends on a beta API (D-035). The conversation only grows at the end, which keeps the prompt cache and Claude's earlier thinking valid, and lets `chat` answer follow-up questions (R-24).

## The tools

| Tool | What it does | Limits |
| --- | --- | --- |
| `query_metrics` | Builds SQL from the semantic layer for metrics, groupings, filters, named filters, a date range and a top N; returns formatted rows, the number of complaints matched, notes and warnings | Names must be defined ones: the tool definitions list them. With a top N it fetches one more row and reports a tie at the cut-off (D-034) |
| `list_values` | Lists the exact values of one dimension that contain some text, with complaint counts and each company's short names | At most 25 values. The text is passed as a query parameter, never written into the SQL |
| `run_sql` | Runs one read-only query, with the reason no metric query could answer it and the metrics it follows | At most 200 rows. Returns `reports_not_findings` and the warnings of the metrics it follows |

Values come back formatted as answers write them (1,234 and 12.3%), so the model copies figures instead of reformatting them. In `run_sql`, columns ending in `_rate` or `_share` are percentages, as in the answer key.

## Keeping generated SQL safe

| Layer | What it stops |
| --- | --- |
| `guard.check_sql` parses the SQL with sqlglot | Anything but one SELECT; any write, setting, `COPY`, `ATTACH` or `PRAGMA`; any table outside `marts.fct_complaints`, `marts.dim_company`, `marts.dim_product` and `marts.dim_date`, including the narratives (D-033); table functions such as `read_csv` |
| `guard.connect` opens the warehouse read-only, with external access off and settings locked | Reading or writing files, attaching databases or installing extensions, even if the parser misjudged a query |
| `guard.execute` stops a query after 30 seconds and fetches a limited number of rows | Runaway queries and huge results |

The metric queries run on the same locked connection. Blocked queries are logged with their reason (US-10).

## The question log

One record per question, in MongoDB Atlas (`query_cf.questions`) when `QUERY_CF_MONGODB_URI` is set, and in `data/logs/questions.jsonl` otherwise or whenever MongoDB can't be reached (D-037).

| Field | Holds |
| --- | --- |
| `question_id`, `conversation_id`, `turn` | Which question, and where it sits in a chat |
| `asked_at`, `seconds` | When it was asked, in UTC, and how long it took |
| `question`, `answer`, `outcome` | The exchange; `outcome` is answered, refused, cut_off, tool_limit, no_text or error |
| `steps` | Every tool call in order: its input, whether it ran or was blocked, its SQL, result rows, warnings and timing |
| `model`, `model_version`, `effort`, `prompt_version` | The model asked for and the one that answered, the effort level, and a fingerprint of the system prompt and tools |
| `usage`, `cost_usd`, `request_ids` | Tokens (cache reads and writes too), the cost in US dollars, and the API request IDs |

## Model and cost

`config/analyst.yaml` pins the model, effort level and limits: Claude Haiku 5.5 (`claude-haiku-5-5`) at `high` effort, at most 12 tool calls per question (D-036). The system prompt and tools are cached, so every request after the first reads them at a tenth of the input price. Each answer prints its cost, worked out from the token counts with the prices in the same file; the README reports measured costs only. Phase 5 re-runs the evaluation set before any model change.

## Commands

```bash
python run_pipeline.py check-accounts   # checks the API key, MongoDB and the warehouse, at no cost
python run_pipeline.py ask "How many credit card complaints did Citi receive in 2025?"
python run_pipeline.py chat             # questions and follow-ups; Enter on an empty line stops
```

The API key and connection string come from `.env` (see `.env.example`), which is never committed.

## Tests

- The safety check allows read-only queries on the marts and blocks 23 kinds of statement and table, each with its reason; the locked connection can't read files or change settings; a slow query is stopped.
- Each tool runs against the seven planted complaints, and every expected number is worked out by hand: formatting, ties at the cut-off, exact company names, the row limit, and errors that say what to change.
- The analyst runs against a fake model that replays scripted responses: tool calls and their results, blocked SQL, refusals, the tool call limit, API errors, and follow-up questions. Every request is checked to only add to the conversation.
- One test drives the real Anthropic SDK with its network replaced by canned responses, so the request the API receives is checked too.
- The system prompt holds all 24 rules and none of the answer key's figures.
- No test calls the Claude API or MongoDB.
