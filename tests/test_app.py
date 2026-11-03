"""Phase 6: the chat app, how answers are shown, the demo data and the usage limits.

The page itself runs under Streamlit's own test runner (AppTest) against the planted
warehouse of test_semantic.py, with the fake model of test_analyst.py, so nothing here calls
the Claude API, MongoDB or a download site.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from test_analyst import WF_COUNT, FakeClient, response, text, tool_call, warehouse  # noqa: F401

import run_pipeline
from query_cf import analyst, guard, present, questionlog, webapp
from query_cf.semantic import load_layer
from query_cf.tools import Toolbox

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app" / "streamlit_app.py"
BY_YEAR = {"metrics": ["complaints"], "group_by": ["received:year"]}
BY_COMPANY = {"metrics": ["complaints"], "group_by": ["company"]}


@pytest.fixture
def toolbox(warehouse):  # noqa: F811
    conn = guard.connect(warehouse)
    yield Toolbox(load_layer(), conn)
    conn.close()


def answered(*results, outcome="answered"):
    steps = [{"tool": tool, "ok": True, "result": result} for tool, result in results]
    return SimpleNamespace(outcome=outcome, steps=steps)


# --- What the tools return for charts --------------------------------------------------------


def test_results_carry_raw_values_that_the_model_never_sees(toolbox):
    result = toolbox.run("query_metrics", BY_YEAR)
    assert result["rows"][0] == ["2016", "3"]
    assert result["values"][0] == ["2016-01-01", 3]
    client = FakeClient(
        response(tool_call("query_metrics", BY_YEAR), stop="tool_use"), response(text("Done."))
    )
    analyst.Analyst(client, toolbox, analyst.load_settings()).ask("By year?")
    sent = json.loads(client.requests[1]["messages"][-1]["content"][0]["content"])
    assert "values" not in sent and "percent_columns" not in sent and "sql" not in sent


# --- How an answer is shown ----------------------------------------------------------------------


def test_one_figure_is_shown_as_a_number(toolbox):
    shown = present.present(answered(("query_metrics", toolbox.run("query_metrics", WF_COUNT))))
    assert shown.kind == "number"
    assert (shown.parts[0].title, shown.parts[0].value) == ("Complaints", "3")


def test_a_trend_is_a_line_chart_labelled_as_answers_write_it(toolbox):
    shown = present.present(answered(("query_metrics", toolbox.run("query_metrics", BY_YEAR))))
    assert shown.kind == "chart" and shown.parts[0].kind == "line"
    trace = shown.parts[0].figure.data[0]
    assert list(trace.x) == ["2016", "2022", "2024", "2025"]
    assert list(trace.y) == [3, 1, 1, 2]


def test_a_ranking_is_a_bar_chart_with_the_largest_at_the_top(toolbox):
    shown = present.present(answered(("query_metrics", toolbox.run("query_metrics", BY_COMPANY))))
    assert shown.parts[0].kind == "bar"
    trace = shown.parts[0].figure.data[0]
    assert trace.orientation == "h"
    assert list(trace.y)[-1] == "Bank A" and list(trace.x)[-1] == 4  # the top bar


def test_rates_are_charted_as_percentages(toolbox):
    rates = toolbox.run(
        "query_metrics", {"metrics": ["timely_response_rate"], "group_by": ["company"]}
    )
    figure = present.present(answered(("query_metrics", rates))).parts[0].figure
    assert figure.layout.xaxis.ticksuffix == "%"


def test_a_year_from_sql_labels_the_rows_rather_than_measuring_them():
    result = {
        "columns": ["year", "total_complaints"],
        "rows": [["2015", "17,297"], ["2016", "21,064"]],
        "values": [[2015, 17297], [2016, 21064]],
        "percent_columns": [],
    }
    part = present.present(answered(("run_sql", result))).parts[0]
    assert part.kind == "line" and list(part.figure.data[0].y) == [17297, 21064]


def test_several_queries_make_a_report(toolbox):
    shown = present.present(
        answered(
            ("query_metrics", toolbox.run("query_metrics", WF_COUNT)),
            ("query_metrics", toolbox.run("query_metrics", BY_YEAR)),
        )
    )
    assert shown.kind == "report"
    assert [p.kind for p in shown.parts] == ["number", "line"]


def test_text_only_when_no_query_ran(toolbox):
    lookup = toolbox.run("list_values", {"dimension": "company", "contains": "fargo"})
    assert present.present(answered()).kind == "text"
    assert present.present(answered(("list_values", lookup))).kind == "text"
    failed = answered(("query_metrics", toolbox.run("query_metrics", WF_COUNT)), outcome="error")
    assert present.present(failed).kind == "text"


# --- Usage limits ---------------------------------------------------------------------------------

LIMITS = webapp.Limits(questions_per_session=2, max_question_chars=40, daily_budget_usd=1.0)


@pytest.mark.parametrize(
    ("question", "asked", "spent", "reason"),
    [
        ("How many?", 0, 0.0, None),
        ("   ", 0, 0.0, "Type a question first."),
        ("x" * 41, 0, 0.0, "under 40 characters"),
        ("How many?", 2, 0.0, "2 questions per visit"),
        ("How many?", 0, None, "can't check today's spending"),
        ("How many?", 0, 1.0, "used today's budget"),
    ],
)
def test_the_demo_refuses_what_its_limits_dont_allow(question, asked, spent, reason):
    refused = webapp.refusal(question, asked, LIMITS, spent)
    assert refused is None if reason is None else reason in refused


def test_todays_spending_counts_only_the_apps_questions_since_midnight(tmp_path):
    log = questionlog.FileLog(tmp_path / "questions.jsonl")
    now = datetime.now(UTC)
    for asked_at, source, cost in [
        (now, "app", 0.25),
        (now, "app", 0.5),
        (now - timedelta(days=1), "app", 9.0),
        (now, None, 7.0),
    ]:
        tags = {"source": source} if source else {}
        log.write({"question_id": "q", "asked_at": asked_at, "cost_usd": cost, "tags": tags})
    assert webapp.spent_today(log) == pytest.approx(0.75)


def test_mongodb_spending_adds_what_fell_back_to_the_file(tmp_path):
    class Collection:
        def aggregate(self, pipeline):
            assert pipeline[0]["$match"]["tags.source"] == "app"
            return [{"_id": None, "total": 0.5}]

    fallback = questionlog.FileLog(tmp_path / "questions.jsonl")
    now = datetime.now(UTC)
    fallback.write(
        {"question_id": "q", "asked_at": now, "cost_usd": 0.1, "tags": {"source": "app"}}
    )
    log = questionlog.MongoLog(Collection(), fallback, "query_cf.questions")
    assert webapp.spent_today(log) == pytest.approx(0.6)


def test_spending_that_cant_be_counted_pauses_the_demo():
    class Unreachable:
        def spent_since(self, since, source):
            raise ConnectionError("no route to the cluster")

    assert webapp.spent_today(Unreachable()) is None


def test_the_app_settings_are_checked(tmp_path):
    settings = webapp.load_app_settings()
    assert settings.limits.questions_per_session == 10
    assert settings.memory_limit == "1GB"
    broken = tmp_path / "app.yaml"
    broken.write_text("limits: {}\n", encoding="utf-8")
    with pytest.raises(webapp.AppError, match="missing or invalid setting"):
        webapp.load_app_settings(broken)


# --- The demo data --------------------------------------------------------------------------------


class Download:
    """A fake streaming HTTP response holding the given bytes."""

    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start : start + chunk_size]


def demo_settings(tmp_path, data: bytes, **changes):
    (tmp_path / "expected.bin").write_bytes(data)
    sha = webapp.sha256_of(tmp_path / "expected.bin")
    settings = replace(
        webapp.load_app_settings(),
        demo_url="https://example.invalid/demo.duckdb",
        demo_sha256=sha,
        demo_file=tmp_path / "demo" / "query_cf_demo.duckdb",
    )
    return replace(settings, **changes)


@pytest.fixture
def no_warehouse(monkeypatch, tmp_path):
    monkeypatch.delenv(webapp.DB_VARIABLE, raising=False)
    monkeypatch.setattr(webapp, "FULL_WAREHOUSE", tmp_path / "missing.duckdb")


def test_the_demo_data_is_downloaded_once_and_checked(no_warehouse, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        webapp.requests, "get", lambda url, **kw: calls.append(url) or Download(b"marts" * 1000)
    )
    settings = demo_settings(tmp_path, b"marts" * 1000)
    path = webapp.database_path(settings)
    assert path == settings.demo_file and path.read_bytes() == b"marts" * 1000
    assert webapp.database_path(settings) == path
    assert len(calls) == 1  # the second start finds the checked file


def test_demo_data_with_the_wrong_checksum_is_never_used(no_warehouse, monkeypatch, tmp_path):
    monkeypatch.setattr(webapp.requests, "get", lambda url, **kw: Download(b"tampered"))
    settings = demo_settings(tmp_path, b"marts")
    with pytest.raises(webapp.AppError, match="SHA-256 doesn't match"):
        webapp.database_path(settings)
    assert not settings.demo_file.exists()
    assert not list(settings.demo_file.parent.glob("*.part"))


def test_without_a_warehouse_or_a_download_the_app_says_what_to_do(no_warehouse, tmp_path):
    with pytest.raises(webapp.AppError, match="doesn't say where to download"):
        webapp.database_path(demo_settings(tmp_path, b"x", demo_url=""))


def test_the_demo_warehouse_holds_only_the_four_mart_tables(warehouse, tmp_path, capsys):  # noqa: F811
    out = tmp_path / "demo.duckdb"
    assert run_pipeline.main(["--db", str(warehouse), "demo-warehouse", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"SHA-256 {webapp.sha256_of(out)}" in printed
    conn = duckdb.connect(str(out), read_only=True)
    try:
        tables = conn.execute(
            "select table_schema, table_name from information_schema.tables order by 1, 2"
        ).fetchall()
        complaints = conn.execute("select count(*) from marts.fct_complaints").fetchone()[0]
    finally:
        conn.close()
    assert set(tables) == set(guard.ALLOWED_TABLES)
    assert complaints == 7


# --- The page -------------------------------------------------------------------------------------


@pytest.fixture
def page(warehouse, tmp_path, monkeypatch):  # noqa: F811
    """The app on the planted warehouse; set .responses before running a question."""
    monkeypatch.chdir(tmp_path)  # the question log goes to data/logs/questions.jsonl here
    monkeypatch.setenv(webapp.DB_VARIABLE, str(warehouse))
    monkeypatch.setenv(analyst.API_KEY_VARIABLE, "test-key")
    monkeypatch.delenv(questionlog.URI_VARIABLE, raising=False)
    client = FakeClient()
    monkeypatch.setattr(webapp, "make_client", lambda: client)
    st.cache_resource.clear()
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.client = client
    yield app
    st.cache_resource.clear()


def log_records(tmp_path):
    lines = (tmp_path / "data" / "logs" / "questions.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_the_page_answers_with_a_number_and_shows_its_working(page, tmp_path):
    page.run()
    assert not page.exception
    page.client.responses += [
        response(tool_call("query_metrics", WF_COUNT), stop="tool_use"),
        response(text("Wells Fargo received 3 complaints.")),
    ]
    page.chat_input[0].set_value("How many complaints did Wells Fargo get, ever?").run()
    assert not page.exception
    assert "Wells Fargo received 3 complaints." in [m.value for m in page.markdown]
    assert [(m.label, m.value) for m in page.metric] == [("Complaints", "3")]
    assert "c.company_name = 'WELLS FARGO & COMPANY'" in page.code[0].value
    [record] = log_records(tmp_path)
    assert record["tags"]["source"] == "app" and len(record["tags"]["session"]) == 32


def test_the_page_draws_a_trend_as_a_chart(page):
    page.run()
    page.client.responses += [
        response(tool_call("query_metrics", BY_YEAR), stop="tool_use"),
        response(text("Complaints peaked in 2016.")),
    ]
    page.chat_input[0].set_value("How did complaints change each year?").run()
    assert not page.exception
    assert len(page.get("plotly_chart")) == 1
    assert len(page.metric) == 0


def test_an_example_question_can_be_asked_with_one_click(page):
    page.run()
    page.client.responses += [response(text("Worst by what measure?"))]
    page.sidebar.button[3].click().run()  # "Which bank has the worst complaints?"
    assert not page.exception
    assert "Worst by what measure?" in [m.value for m in page.markdown]
    assert page.client.requests[0]["messages"][-1]["content"] == (
        "Which bank has the worst complaints?"
    )


def test_the_page_stops_at_the_question_limit(page, monkeypatch):
    limited = replace(
        webapp.load_app_settings(),
        limits=webapp.Limits(questions_per_session=1, max_question_chars=500, daily_budget_usd=1.0),
    )
    monkeypatch.setattr(webapp, "load_app_settings", lambda *a: limited)
    page.run()
    page.client.responses += [response(text("Asked."))]
    page.chat_input[0].set_value("First question?").run()
    page.chat_input[0].set_value("Second question?").run()
    assert not page.exception
    assert "1 questions per visit" in page.warning[0].value
    assert len(page.client.requests) == 1


def test_the_page_explains_a_missing_api_key(page, monkeypatch):
    monkeypatch.delenv(analyst.API_KEY_VARIABLE)
    page.run()
    assert "ANTHROPIC_API_KEY isn't set" in page.error[0].value
