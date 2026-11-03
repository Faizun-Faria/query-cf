"""Run the QueryCF pipeline from one command.

    python run_pipeline.py ingest      # phase 1: land every enabled source's exports, in order
    python run_pipeline.py load        # phase 2: copy each landed batch into bronze
    python run_pipeline.py transform   # phase 2: dbt builds and tests the warehouse
    python run_pipeline.py all         # ingest, load and transform in one go
    python run_pipeline.py status      # show every ingestion attempt and batch load

    python run_pipeline.py query complaints --by received:year --where company="Wells Fargo"
    python run_pipeline.py check-semantic   # phase 3: run every definition on the warehouse
    python run_pipeline.py catalog          # phase 3: regenerate docs/08-metric-catalog.md

    python run_pipeline.py check-accounts   # phase 4: test the Claude API key and MongoDB
    python run_pipeline.py ask "How many credit card complaints did Citi receive in 2025?"
    python run_pipeline.py chat             # phase 4: questions and follow-ups in a row
    python run_pipeline.py evaluate         # phase 5: score the analyst on evals/questions.csv

    python run_pipeline.py ingest --export CCDB_Export_21_August_2026.zip

Raw files land in data/raw; the audit tables and the warehouse live in
data/warehouse/query_cf.duckdb. Set QUERY_CF_RAW=s3://bucket/prefix to land
raw files in S3 instead. The analyst reads ANTHROPIC_API_KEY and QUERY_CF_MONGODB_URI
from .env (see .env.example).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import duckdb

from query_cf import audit, load, semantic
from query_cf.config import DEFAULT_CONFIG_DIR, ConfigError, load_sources
from query_cf.ingest import ingest_source
from query_cf.storage import open_store

DEFAULT_RAW = os.environ.get("QUERY_CF_RAW", "data/raw")
DEFAULT_DB = os.environ.get("QUERY_CF_DB", "data/warehouse/query_cf.duckdb")
CATALOG = Path(__file__).resolve().parent / "docs" / "08-metric-catalog.md"
QUESTIONS = Path(__file__).resolve().parent / "evals" / "questions.csv"
REPORT = Path(__file__).resolve().parent / "docs" / "12-evaluation-results.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QueryCF pipeline")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"DuckDB file (default {DEFAULT_DB})")
    parser.add_argument(
        "--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, help="folder of source configs"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    raw = argparse.ArgumentParser(add_help=False)
    raw.add_argument(
        "--raw",
        default=DEFAULT_RAW,
        help=f"raw zone: a folder or s3://bucket/prefix ({DEFAULT_RAW})",
    )

    ingest = commands.add_parser(
        "ingest", parents=[raw], help="land exports in the raw zone and audit them"
    )
    ingest.add_argument("--source", action="append", help="only this source (repeatable)")
    ingest.add_argument("--export", action="append", help="only this export file (repeatable)")
    ingest.add_argument(
        "--force",
        action="store_true",
        help="fetch landed exports again; they land again only if their bytes changed",
    )

    commands.add_parser("load", help="copy each landed batch into the bronze table")
    transform = commands.add_parser("transform", help="build and test the warehouse with dbt")
    transform.add_argument("--select", help="only these dbt models (dbt selection syntax)")
    commands.add_parser("all", parents=[raw], help="ingest, load and transform")
    commands.add_parser("status", help="show every ingestion attempt and batch load")

    query = commands.add_parser("query", help="answer a metric question from the semantic layer")
    query.add_argument("metrics", nargs="+", help="metric names, such as complaints")
    query.add_argument(
        "--by", action="append", default=[], help="dimension to group by; received:month for time"
    )
    query.add_argument(
        "--where",
        action="append",
        default=[],
        metavar="DIMENSION=VALUE",
        help="keep only matching complaints; repeat a dimension to allow several values",
    )
    query.add_argument("--segment", action="append", default=[], help="named filter to apply")
    query.add_argument("--from", dest="received_from", type=date.fromisoformat, help="YYYY-MM-DD")
    query.add_argument("--to", dest="received_to", type=date.fromisoformat, help="YYYY-MM-DD")
    query.add_argument("--top", type=int, help="only the N rows with the highest first metric")
    query.add_argument("--sql-only", action="store_true", help="print the SQL without running it")

    catalog = commands.add_parser("catalog", help="regenerate the metric catalog document")
    catalog.add_argument("--out", type=Path, default=CATALOG)
    commands.add_parser("check-semantic", help="run every semantic definition on the warehouse")

    commands.add_parser(
        "check-accounts", help="check the Claude API key, MongoDB and the warehouse, at no cost"
    )
    ask = commands.add_parser("ask", help="ask the AI analyst one question in plain English")
    ask.add_argument("question")
    commands.add_parser("chat", help="ask the AI analyst questions and follow-ups in a row")
    evaluate = commands.add_parser(
        "evaluate", help="ask the analyst every evaluation question and score the answers"
    )
    evaluate.add_argument("--questions", type=Path, default=QUESTIONS)
    evaluate.add_argument(
        "--runs",
        type=int,
        default=1,
        help="ask every question this many times, since answers vary (default 1)",
    )
    evaluate.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ID",
        help="only this question, such as Q04 (repeatable); the report isn't rewritten",
    )
    evaluate.add_argument("--report", type=Path, help=f"results file (default {REPORT.name})")
    return parser


def enabled_sources(args: argparse.Namespace) -> list:
    return [s for s in load_sources(args.config_dir).values() if s.enabled]


def cmd_ingest(args: argparse.Namespace) -> int:
    sources = load_sources(args.config_dir)
    wanted = args.source or list(sources)
    unknown = sorted(set(wanted) - set(sources))
    if unknown:
        print(f"Unknown source: {', '.join(unknown)}. Known: {', '.join(sources)}")
        return 2
    selected = [sources[name] for name in wanted]

    if args.export:
        known = {e for source in selected for e in source.exports}
        missing = sorted(set(args.export) - known)
        if missing:
            print(f"Not in any selected source's config: {', '.join(missing)}")
            return 2

    store = open_store(args.raw)
    failed = 0
    conn = audit.connect(args.db)
    try:
        for source in selected:
            if not source.enabled:
                print(f"{source.name}: disabled in config, skipped")
                continue
            print(f"{source.name}: {source.description}")
            results = ingest_source(source, store, conn, only=args.export, force=args.force)
            failed += sum(r.status == audit.FAILED for r in results)
    finally:
        conn.close()

    if failed:
        print(f"{failed} export(s) failed; see `python run_pipeline.py status`.")
        return 1
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    failed = missing = 0
    conn = audit.connect(args.db)
    try:
        for source in enabled_sources(args):
            print(f"{source.name}: loading into {load.bronze_table(source)}")
            results = load.load_source(source, conn, work_dir=Path(args.db).parent)
            failed += sum(r.status == audit.FAILED for r in results)
            missing += sum(r.status == load.MISSING for r in results)
    finally:
        conn.close()

    if failed:
        print(f"{failed} batch(es) failed to load; see `python run_pipeline.py status`.")
    if missing:
        print(f"{missing} export(s) in the config have no landed batch; run ingest first.")
    return 1 if failed or missing else 0


def cmd_transform(args: argparse.Namespace) -> int:
    from query_cf import transform  # imports dbt, which takes a few seconds

    dbt_args = ["build"] + (["--select", args.select] if args.select else [])
    ok = transform.run_dbt(args.db, dbt_args)
    incomplete = transform.incomplete_batches(args.db)
    for batch_id, export, audited, loaded, in_marts, quarantined in incomplete:
        if batch_id is None:
            print(f"INCOMPLETE {export}: listed in the config, but no batch of it has landed")
            continue
        print(
            f"INCOMPLETE {export} (batch {batch_id}): audited {audited:,}, loaded {loaded:,}, "
            f"in marts {in_marts:,} + quarantined {quarantined:,}"
        )
    return 0 if ok and not incomplete else 1


def cmd_all(args: argparse.Namespace) -> int:
    args.source, args.export, args.force, args.select = None, None, False, None
    for step in (cmd_ingest, cmd_load, cmd_transform):
        if code := step(args):
            return code
    return 0


def open_warehouse(db: str) -> duckdb.DuckDBPyConnection | None:
    if not Path(db).exists():
        print(f"No warehouse at {db}. Run: python run_pipeline.py all")
        return None
    return duckdb.connect(db, read_only=True)


def cmd_query(args: argparse.Namespace) -> int:
    filters: dict[str, list[str]] = {}
    for item in args.where:
        name, sep, value = item.partition("=")
        if not sep:
            print(f"--where needs DIMENSION=VALUE, got {item!r}")
            return 2
        filters.setdefault(name.strip(), []).append(value.strip().strip('"'))
    request = semantic.MetricQuery(
        metrics=tuple(args.metrics),
        by=tuple(args.by),
        filters={name: tuple(values) for name, values in filters.items()},
        segments=tuple(args.segment),
        received_from=args.received_from,
        received_to=args.received_to,
        top=args.top,
    )
    try:
        compiled = semantic.compile_query(semantic.load_layer(), request)
    except semantic.SemanticError as exc:
        print(f"Query error: {exc}")
        return 2
    print(compiled.sql)
    if args.sql_only:
        return 0

    conn = open_warehouse(args.db)
    if conn is None:
        return 1
    try:
        rows = semantic.run_query(conn, compiled)
    except duckdb.CatalogException:
        print("\nThe marts aren't built yet. Run: python run_pipeline.py transform")
        return 1
    finally:
        conn.close()

    percent = set(compiled.percent_columns)
    print()
    print_table(
        list(compiled.columns),
        [
            tuple(
                semantic.format_value(v, c in percent, c)
                for c, v in zip(compiled.columns, row, strict=True)
            )
            for row in rows
        ],
    )
    for note in compiled.notes:
        print(f"\nNote: {note}")
    for caveat in compiled.caveats:
        print(f"\nWarning: {caveat}")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(semantic.render_catalog(semantic.load_layer()), encoding="utf-8")
    print(f"Catalog written to {args.out}")
    return 0


def cmd_check_semantic(args: argparse.Namespace) -> int:
    layer = semantic.load_layer()
    conn = open_warehouse(args.db)
    if conn is None:
        return 1
    try:
        rows, problems = semantic.check_layer(conn, layer)
    finally:
        conn.close()
    print_table(["kind", "name", "result"], rows)
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("\nEvery definition runs on the warehouse.")
    return 0


def make_client():
    """The Claude API client; it reads ANTHROPIC_API_KEY. Tests replace this function."""
    import anthropic

    return anthropic.Anthropic()


def start_analyst(args: argparse.Namespace):
    """The analyst over the warehouse, or None after saying what's missing."""
    from query_cf import analyst, guard, questionlog
    from query_cf.tools import Toolbox

    settings = analyst.load_settings()
    if not os.environ.get(analyst.API_KEY_VARIABLE, "").strip():
        print(f"{analyst.API_KEY_VARIABLE} isn't set. Add it to .env (see .env.example).")
        return None
    if not Path(args.db).exists():
        print(f"No warehouse at {args.db}. Run: python run_pipeline.py all")
        return None
    conn = guard.connect(args.db)
    try:
        conn.execute("select 1 from marts.fct_complaints limit 1")
    except duckdb.CatalogException:
        conn.close()
        print("The marts aren't built yet. Run: python run_pipeline.py transform")
        return None
    log, problem = questionlog.open_log(
        settings.log_database, settings.log_collection, settings.log_file
    )
    if problem:
        print(f"Note: {problem}; questions are logged to {settings.log_file}")
    return analyst.Analyst(make_client(), Toolbox(semantic.load_layer(), conn), settings, log)


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def print_answer(answer) -> None:
    print(answer.text)
    print("\nHow this was worked out")
    if not answer.steps:
        print("  No queries were run.")
    for n, step in enumerate(answer.steps, 1):
        result, tool = step.get("result") or {}, step["tool"]
        if not step["ok"]:
            summary = step["error"]
        elif tool == "list_values":
            found = plural(len(result["values"]), f"{result['dimension']} value")
            summary = f"{found} containing '{result['contains']}'"
        elif tool == "run_sql":
            rows = plural(result["rows_returned"], "row")
            summary = f"{rows}. Why SQL: {step['input'].get('reason', '')}"
        else:
            rows = plural(result["rows_returned"], "row")
            summary = f"{rows} from {result['complaints_matched']} complaints"
        print(f"  {n}. {tool}: {summary}")
        if step.get("sql") or tool == "run_sql":
            sql = step.get("sql") or step["input"].get("sql", "")
            print("\n".join(f"       {line}" for line in sql.splitlines()))
    usage = answer.usage
    prompt = sum(
        usage[k] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    print(
        f"\n{answer.model_version or 'No model'} answered in "
        f"{plural(usage['requests'], 'request')}: "
        f"{prompt:,} input tokens ({usage['cache_read_input_tokens']:,} from the cache) and "
        f"{usage['output_tokens']:,} output tokens, ${answer.cost_usd:.4f}"
    )
    if answer.log_location:
        print(f"Logged to {answer.log_location}")


def cmd_ask(args: argparse.Namespace) -> int:
    analyst = start_analyst(args)
    if analyst is None:
        return 1
    try:
        answer = analyst.ask(args.question)
    finally:
        analyst.toolbox.conn.close()
    print_answer(answer)
    return 0 if answer.outcome == "answered" else 1


def cmd_chat(args: argparse.Namespace) -> int:
    analyst = start_analyst(args)
    if analyst is None:
        return 1
    print("Ask about the complaints archive. Follow-up questions build on earlier answers.")
    print("Press Enter on an empty line to finish.")
    try:
        while True:
            try:
                question = input("\nYou: ").strip()
            except EOFError:
                break
            if question.lower() in ("", "exit", "quit"):
                break
            print()
            print_answer(analyst.ask(question))
    finally:
        analyst.toolbox.conn.close()
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from query_cf import evaluate

    try:
        questions = evaluate.load_questions(args.questions)
    except evaluate.EvaluationError as exc:
        print(f"Questions error: {exc}")
        return 2
    if args.only:
        unknown = sorted(set(args.only) - {q.id for q in questions})
        if unknown:
            print(f"Not in {args.questions.name}: {', '.join(unknown)}")
            return 2
        questions = [q for q in questions if q.id in args.only]
    if not 1 <= args.runs <= 10:
        print("--runs must be from 1 to 10")
        return 2

    analyst = start_analyst(args)
    if analyst is None:
        return 1
    settings = analyst.settings
    asking = plural(len(questions), "question")
    times = f", {args.runs} times each" if args.runs > 1 else ""
    print(f"Asking {asking}{times} with {settings.model} at {settings.effort} effort")
    try:
        results = evaluate.run(questions, analyst, analyst.toolbox.layer, runs=args.runs)
    finally:
        analyst.toolbox.conn.close()

    report = args.report or (None if args.only else REPORT)
    passed = sum(r.score.passed for r in results)
    cost = sum(r.answer.cost_usd for r in results)
    if args.runs == 1:
        print(f"\nPassed {passed} of {len(results)}. The run cost ${cost:.4f}.")
    else:
        grouped = evaluate.by_question(results)
        steady = sum(all(r.score.passed for r in rs) for rs in grouped.values())
        print(
            f"\nPassed every run: {steady} of {len(grouped)} questions. Pass rate: {passed} of "
            f"{len(results)} answers. The run cost ${cost:.4f}."
        )
    for r in results:
        which = f"{r.question.id} run {r.run}" if args.runs > 1 else r.question.id
        if not r.score.passed:
            print(f"  {which}: {'; '.join(r.score.misses)}")
            if report is None:  # nothing else shows the answer, so show it here
                print("\n".join(f"      {line}" for line in r.answer.text.splitlines()))
        if r.score.unsupported:
            print(f"  {which}: figures no tool returned: {', '.join(r.score.unsupported)}")

    errors = [f"{r.question.id} (run {r.run})" for r in results if r.answer.outcome == "error"]
    if errors:
        print(f"\nThe Claude API failed on {', '.join(errors)}, so no report was written.")
        return 1
    if report is None:
        print(
            "\nOnly some questions ran, so the report wasn't rewritten. Add --report to write one."
        )
        return 0
    report.parent.mkdir(parents=True, exist_ok=True)
    text = evaluate.render_report(results, settings, analyst.version, datetime.now(UTC))
    report.write_text(text, encoding="utf-8")
    print(f"Results written to {report}")
    return 0


def cmd_check_accounts(args: argparse.Namespace) -> int:
    import anthropic

    from query_cf import analyst, guard, questionlog

    settings = analyst.load_settings()
    problems = 0
    if not os.environ.get(analyst.API_KEY_VARIABLE, "").strip():
        print(f"Claude API: {analyst.API_KEY_VARIABLE} isn't set. Add it to .env.")
        problems += 1
    else:
        try:
            model = make_client().models.retrieve(settings.model)
            print(f"Claude API: OK, {model.id} ({model.display_name}) is available to this key")
        except anthropic.APIError as exc:
            print(f"Claude API: {analyst.describe_api_error(exc)}")
            problems += 1

    uri = os.environ.get(questionlog.URI_VARIABLE, "").strip()
    if not uri:
        print(f"MongoDB: {questionlog.URI_VARIABLE} isn't set. Add it to .env.")
        problems += 1
    else:
        try:
            questionlog.check_mongo(uri, settings.log_database)
            print(f"MongoDB: OK, the database user can write to {settings.log_database}")
        except Exception as exc:  # every failure is reported, whatever the driver raises
            print(f"MongoDB: {questionlog.describe_mongo_error(exc)}")
            problems += 1

    if not Path(args.db).exists():
        print(f"Warehouse: no warehouse at {args.db}. Run: python run_pipeline.py all")
        problems += 1
    else:
        conn = guard.connect(args.db)
        try:
            count = conn.execute("select count(*) from marts.fct_complaints").fetchone()[0]
            print(f"Warehouse: OK, {count:,} complaints in marts.fct_complaints")
        except duckdb.CatalogException:
            print("Warehouse: the marts aren't built yet. Run: python run_pipeline.py transform")
            problems += 1
        finally:
            conn.close()

    if problems:
        print(f"\n{problems} problem(s) to fix before asking questions.")
        return 1
    print('\nEverything is ready. Try: python run_pipeline.py ask "How many complaints in 2025?"')
    return 0


def print_table(columns: list[str], rows: list[tuple]) -> None:
    cells = [columns] + [["" if v is None else str(v) for v in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
    for n, row in enumerate(cells):
        print("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))
        if n == 0:
            print("  ".join("-" * width for width in widths))


def cmd_status(args: argparse.Namespace) -> int:
    conn = audit.connect(args.db)
    try:
        attempts = audit.summary(conn)
        loads = load.summary(conn)
    finally:
        conn.close()
    if not attempts[1]:
        print("No ingestion attempts yet. Run: python run_pipeline.py ingest")
        return 0

    print("Ingestion attempts")
    print_table(*attempts)
    print("\nBatch loads")
    if loads[1]:
        print_table(*loads)
    else:
        print("None yet. Run: python run_pipeline.py load")
    return 0


COMMANDS = {
    "ingest": cmd_ingest,
    "load": cmd_load,
    "transform": cmd_transform,
    "all": cmd_all,
    "status": cmd_status,
    "query": cmd_query,
    "catalog": cmd_catalog,
    "check-semantic": cmd_check_semantic,
    "check-accounts": cmd_check_accounts,
    "ask": cmd_ask,
    "chat": cmd_chat,
    "evaluate": cmd_evaluate,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 2
    except semantic.SemanticError as exc:
        print(f"Semantic layer error: {exc}")
        return 2


def load_env() -> None:
    """Read .env next to this file; variables already set keep their values."""
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)


if __name__ == "__main__":
    load_env()
    if hasattr(sys.stdout, "reconfigure"):  # never crash on a character the console can't show
        sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
