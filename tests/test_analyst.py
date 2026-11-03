"""Phase 4: the AI analyst, its tools and its question log.

The tools run against the seven planted complaints of test_semantic.py. The model is a
fake that replays scripted responses, so no test calls the Claude API, MongoDB or anything
else outside this machine, and every expected number can be worked out by hand.
"""

import copy
import csv
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pymongo import errors as mongo_errors
from test_semantic import NEW, OLD
from test_warehouse import build_warehouse

import run_pipeline
from query_cf import analyst, guard, questionlog
from query_cf.analyst import Analyst, AnalystError, load_settings
from query_cf.semantic import load_layer
from query_cf.tools import Toolbox, ToolError

ROOT = Path(__file__).resolve().parent.parent
WF = "WELLS FARGO & COMPANY"


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    db, ok = build_warehouse(tmp_path_factory.mktemp("analyst"), old_rows=OLD, new_rows=NEW)
    assert ok, "dbt build failed; see the dbt output above"
    return db


@pytest.fixture(scope="module")
def layer():
    return load_layer()


@pytest.fixture
def toolbox(warehouse, layer):
    conn = guard.connect(warehouse)
    yield Toolbox(layer, conn)
    conn.close()


# --- A fake Claude API ------------------------------------------------------------------------


def usage(fresh=1000, output=100, written=0, read=0):
    return SimpleNamespace(
        input_tokens=fresh,
        output_tokens=output,
        cache_creation_input_tokens=written,
        cache_read_input_tokens=read,
    )


def text(words):
    return SimpleNamespace(type="text", text=words)


def tool_call(name, tool_input, call_id="toolu_1"):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=tool_input)


def response(*content, stop="end_turn", tokens=None):
    return SimpleNamespace(
        model="claude-haiku-5-5",
        stop_reason=stop,
        content=list(content),
        usage=tokens or usage(),
        _request_id="req_test",
    )


class FakeClient:
    """Replays scripted responses and keeps a copy of every request it was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self
        self.models = SimpleNamespace(
            retrieve=lambda model: SimpleNamespace(id=model, display_name="Claude Haiku 5.5")
        )

    def create(self, **request):
        self.requests.append(copy.deepcopy(request))
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class ListLog:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)
        return f"the test log (question {record['question_id']})"


WF_COUNT = {
    "metrics": ["complaints"],
    "filters": [{"dimension": "company", "values": ["Wells Fargo"]}],
}


# --- query_metrics ------------------------------------------------------------------------------


def test_query_metrics_answers_from_the_semantic_layer(toolbox):
    result = toolbox.run("query_metrics", WF_COUNT)
    assert result["rows"] == [["3"]]
    assert result["complaints_matched"] == "3"
    assert f"c.company_name = '{WF}'" in result["sql"]
    assert f'"Wells Fargo" means {WF}' in result["notes"]
    assert any(
        w.startswith("Complaints are unverified consumer reports") for w in result["warnings"]
    )


def test_query_metrics_formats_values_as_answers_write_them(toolbox):
    result = toolbox.run(
        "query_metrics",
        {"metrics": ["complaints", "timely_response_rate"], "group_by": ["received:year"]},
    )
    assert result["columns"] == ["received_year", "complaints", "timely_response_rate"]
    assert result["rows"] == [
        ["2016", "3", "66.7%"],
        ["2022", "1", "100.0%"],
        ["2024", "1", "100.0%"],
        ["2025", "2", "100.0%"],
    ]


def test_a_tie_at_the_cut_off_is_reported(toolbox):
    by_year = {"metrics": ["complaints"], "group_by": ["received:year"]}
    tied = toolbox.run("query_metrics", {**by_year, "top": 3})
    assert tied["rows"] == [["2016", "3"], ["2025", "2"], ["2022", "1"]]
    assert "The next row also has 1" in tied["tie_at_cutoff"]
    assert "tie_at_cutoff" not in toolbox.run("query_metrics", {**by_year, "top": 2})


def test_complaints_matched_counts_the_filters_not_the_groups(toolbox):
    result = toolbox.run(
        "query_metrics",
        {"metrics": ["complaints"], "group_by": ["product_group"], "received_from": "2020-01-01"},
    )
    assert result["complaints_matched"] == "4"
    assert result["rows_returned"] == 3


@pytest.mark.parametrize(
    ("tool_input", "message"),
    [
        ({"metrics": ["complaint"]}, "Did you mean complaints"),
        ({"metrics": ["complaints"], "received_from": "2024/01/01"}, "YYYY-MM-DD"),
        ({"metrics": ["complaints"], "top": 0}, "1 or more"),
        ({"metrics": ["complaints"], "filters": [{"dimension": "state"}]}, "dimension, values"),
        ({"metrics": ["complaints"], "where": "x"}, "unknown input where"),
        ({"metrics": "complaints"}, "must be a list"),
    ],
)
def test_query_metrics_explains_what_to_change(toolbox, tool_input, message):
    with pytest.raises(ToolError, match=message):
        toolbox.run("query_metrics", tool_input)


# --- list_values --------------------------------------------------------------------------------


def test_list_values_finds_exact_names_with_their_short_names(toolbox):
    result = toolbox.run("list_values", {"dimension": "company", "contains": "FARGO"})
    assert result["values"] == [{"value": WF, "complaints": "3", "short_names": ["Wells Fargo"]}]
    assert result["more_values"] is False
    everything = toolbox.run("list_values", {"dimension": "company"})
    assert [v["value"] for v in everything["values"]] == ["Bank A", WF]


def test_list_values_reads_text_as_a_value_not_as_sql(toolbox):
    result = toolbox.run("list_values", {"dimension": "issue", "contains": "' or 1=1 --"})
    assert result["values"] == []


def test_list_values_only_lists_category_dimensions(toolbox):
    with pytest.raises(ToolError, match="works on these dimensions"):
        toolbox.run("list_values", {"dimension": "received"})


# --- run_sql ------------------------------------------------------------------------------------

TIMELY_BY_COMPANY = (
    "select c.company_name, 100.0 * count(*) filter (where f.is_timely_response) / count(*) "
    "as timely_response_rate from marts.fct_complaints as f join marts.dim_company as c "
    "on c.company_key = f.company_key group by 1 order by 1"
)


def test_run_sql_runs_a_checked_query_and_formats_rates(toolbox, layer):
    result = toolbox.run(
        "run_sql",
        {"sql": TIMELY_BY_COMPANY, "reason": "lowest first", "follows": ["timely_response_rate"]},
    )
    assert result["rows"] == [["Bank A", "75.0%"], [WF, "100.0%"]]
    assert result["truncated"] is False
    assert result["warnings"][0] == layer.caveats["reports_not_findings"]


def test_run_sql_blocks_anything_but_reading_the_marts(toolbox):
    with pytest.raises(guard.BlockedQuery, match="complaint_narratives isn't an allowed table"):
        toolbox.run(
            "run_sql",
            {"sql": "select * from marts.complaint_narratives", "reason": "x", "follows": []},
        )


@pytest.mark.parametrize(
    ("tool_input", "message"),
    [
        ({"sql": TIMELY_BY_COMPANY, "reason": " ", "follows": []}, "give the reason"),
        ({"sql": TIMELY_BY_COMPANY, "reason": "x", "follows": ["speed"]}, "unknown metric"),
        ({"sql": "select nonsense from marts.dim_date", "reason": "x", "follows": []}, "rejected"),
    ],
)
def test_run_sql_needs_a_reason_known_metrics_and_valid_sql(toolbox, tool_input, message):
    with pytest.raises(ToolError, match=message):
        toolbox.run("run_sql", tool_input)


def test_run_sql_returns_at_most_the_row_limit(layer, warehouse):
    conn = guard.connect(warehouse)
    try:
        result = Toolbox(layer, conn, max_rows=2).run(
            "run_sql", {"sql": "select * from marts.fct_complaints", "reason": "x", "follows": []}
        )
    finally:
        conn.close()
    assert result["rows_returned"] == 2
    assert result["truncated"] is True


def test_tool_definitions_offer_only_defined_names(toolbox, layer):
    tools = {t["name"]: t for t in toolbox.definitions()}
    assert list(tools) == ["query_metrics", "list_values", "run_sql"]
    metric_enum = tools["query_metrics"]["input_schema"]["properties"]["metrics"]["items"]["enum"]
    assert metric_enum == list(layer.metrics)
    assert "complaint_narratives" not in json.dumps(tools)
    assert toolbox.definitions() == toolbox.definitions()  # the same every time, so it caches


# --- The system prompt ------------------------------------------------------------------------


def test_the_rules_come_from_the_rules_document_without_references():
    rules = analyst.rules_section()
    assert rules.startswith("## Rules")
    assert re.findall(r"\*\*(R-\d\d)\.\*\*", rules) == [f"R-{n:02d}" for n in range(1, 25)]
    assert not re.search(r"\bQ\d\d\b|D-\d{3}|US-\d{2}", rules)
    assert "## Enforced in code" not in rules


def test_the_system_prompt_holds_no_evaluation_answers(layer):
    prompt = analyst.build_system_prompt(layer, analyst.rules_section())
    assert prompt == analyst.build_system_prompt(layer, analyst.rules_section())
    assert "| `relief_rate` (Relief rate) |" in prompt
    with open(ROOT / "evals" / "questions.csv", encoding="utf-8-sig") as f:
        answers = " ".join(row["expected_answer"] for row in csv.DictReader(f))
    # Every count with a thousands separator and every rate in the answer key, such as 7,058.
    figures = set(re.findall(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\.\d%", answers))
    assert len(figures) > 50
    assert sorted(f for f in figures if f in prompt) == []


def test_settings_are_checked(tmp_path):
    settings = (ROOT / "config" / "analyst.yaml").read_text(encoding="utf-8")
    path = tmp_path / "analyst.yaml"
    path.write_text(settings.replace("effort: high", "effort: extreme"), encoding="utf-8")
    with pytest.raises(AnalystError, match="effort must be one of"):
        load_settings(path)
    path.write_text(settings.replace("max_tokens:", "maximum_tokens:"), encoding="utf-8")
    with pytest.raises(AnalystError, match="max_tokens"):
        load_settings(path)


def test_cost_uses_the_price_card_for_the_prompt_length():
    settings = load_settings()
    short = analyst.request_cost(
        usage(fresh=1000, written=10_000, read=50_000, output=500), settings
    )
    assert short == pytest.approx((1000 * 0.10 + 10_000 * 0.125 + 50_000 * 0.01 + 500 * 0.50) / 1e6)
    long = analyst.request_cost(usage(fresh=150_000, output=0), settings)
    assert long == pytest.approx(150_000 * 0.50 / 1e6)


# --- The analyst loop ---------------------------------------------------------------------------


def make_analyst(toolbox, *responses, log=None, **settings):
    client = FakeClient(*responses)
    chosen = replace(load_settings(), **settings)
    return Analyst(client, toolbox, chosen, log), client


def test_the_analyst_runs_tools_and_logs_the_whole_exchange(toolbox):
    log = ListLog()
    agent, client = make_analyst(
        toolbox,
        response(tool_call("query_metrics", WF_COUNT), stop="tool_use"),
        response(text("Wells Fargo received 3 complaints.")),
        log=log,
    )
    answer = agent.ask("How many complaints did Wells Fargo get, ever?")

    assert answer.text == "Wells Fargo received 3 complaints."
    assert answer.outcome == "answered"
    reply = client.requests[1]["messages"][-1]["content"][0]
    assert reply["tool_use_id"] == "toolu_1" and "is_error" not in reply
    sent = json.loads(reply["content"])
    assert sent["rows"] == [["3"]] and "sql" not in sent  # the SQL is shown to people, not resent

    [record] = log.records
    assert answer.log_location == f"the test log (question {record['question_id']})"
    assert record["question"] == "How many complaints did Wells Fargo get, ever?"
    assert record["answer"] == answer.text
    assert record["model"] == record["model_version"] == "claude-haiku-5-5"
    assert record["effort"] == "high"
    assert record["usage"]["requests"] == 2
    assert record["cost_usd"] == pytest.approx(2 * (1000 * 0.10 + 100 * 0.50) / 1e6)
    [step] = record["steps"]
    assert step["tool"] == "query_metrics" and step["ok"] is True
    assert f"c.company_name = '{WF}'" in step["sql"]
    assert step["result"]["complaints_matched"] == "3"
    assert len(record["prompt_version"]) == 12


def test_requests_cache_the_prompt_and_only_ever_add_to_the_conversation(toolbox):
    agent, client = make_analyst(
        toolbox,
        response(tool_call("query_metrics", WF_COUNT), stop="tool_use"),
        response(text("Wells Fargo received 3 complaints.")),
        response(text("Bank A received 4.")),
    )
    agent.ask("How many complaints did Wells Fargo get, ever?")
    agent.ask("And Bank A?")

    first = client.requests[0]
    assert first["model"] == "claude-haiku-5-5"
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"] == {"effort": "high"}
    assert "temperature" not in first
    for earlier, later in zip(client.requests, client.requests[1:], strict=False):
        assert later["system"] == earlier["system"] and later["tools"] == earlier["tools"]
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
    assert client.requests[2]["messages"][-1] == {"role": "user", "content": "And Bank A?"}


def test_blocked_sql_and_tool_errors_go_back_to_the_model(toolbox):
    log = ListLog()
    agent, client = make_analyst(
        toolbox,
        response(
            tool_call(
                "run_sql",
                {"sql": "drop table marts.dim_date", "reason": "x", "follows": []},
                call_id="toolu_a",
            ),
            tool_call("query_metrics", {"metrics": ["nope"]}, call_id="toolu_b"),
            stop="tool_use",
        ),
        response(text("I couldn't answer that.")),
        log=log,
    )
    answer = agent.ask("Delete the dates")
    blocked, failed = client.requests[1]["messages"][-1]["content"]
    assert blocked["is_error"] and blocked["content"].startswith("Blocked: only SELECT")
    assert failed["is_error"] and "unknown metric 'nope'" in failed["content"]
    assert [s["blocked"] for s in answer.steps] == [True, False]
    assert log.records[0]["steps"][0]["error"].startswith("Blocked:")


def test_a_refusal_is_reported_and_left_out_of_the_conversation(toolbox):
    agent, _ = make_analyst(toolbox, response(text(""), stop="refusal"))
    answer = agent.ask("Something the model declines")
    assert answer.outcome == "refused"
    assert answer.text == analyst.REFUSED
    assert agent.messages == [{"role": "user", "content": "Something the model declines"}]


def test_the_real_sdk_sends_what_the_analyst_builds(toolbox):
    """The Anthropic SDK itself, with its network replaced by canned API responses."""
    bodies = []
    replies = [
        {
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                {"type": "tool_use", "id": "toolu_1", "name": "query_metrics", "input": WF_COUNT},
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [{"type": "text", "text": "Wells Fargo received 3."}],
            "stop_reason": "end_turn",
        },
    ]

    def api(request):
        bodies.append(json.loads(request.content))
        reply = replies.pop(0)
        message = {"id": "msg_1", "type": "message", "role": "assistant"}
        message.update(model="claude-haiku-5-5", stop_sequence=None, **reply)
        message["usage"] = {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 900}
        return httpx2.Response(200, json=message, headers={"request-id": "req_1"})

    client = anthropic.Anthropic(
        api_key="test-key",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api)),
        max_retries=0,
    )
    answer = Analyst(client, toolbox, load_settings()).ask("Wells Fargo, ever?")

    assert answer.text == "Wells Fargo received 3."
    assert answer.usage["cache_read_input_tokens"] == 1800
    first, second = bodies
    assert first["output_config"] == {"effort": "high"} and len(first["tools"]) == 3
    # The thinking block goes back unchanged, followed by the tool call and its result.
    assert second["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig-1",
    }
    assert second["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"


def test_the_tool_call_limit_ends_a_question(toolbox):
    calls = [tool_call("query_metrics", WF_COUNT, call_id=f"toolu_{n}") for n in range(2)]
    agent, client = make_analyst(
        toolbox,
        response(*calls, stop="tool_use"),
        response(tool_call("query_metrics", WF_COUNT, call_id="toolu_9"), stop="tool_use"),
        max_tool_calls=1,
    )
    answer = agent.ask("Keep querying")
    assert answer.outcome == "tool_limit"
    assert len(answer.steps) == 1
    limited = client.requests[1]["messages"][-1]["content"][1]
    assert limited["is_error"] and "used all its tool calls" in limited["content"]
    # Every tool call in the conversation has its result, so a follow-up stays valid.
    assert agent.messages[-1]["role"] == "user"


def test_api_errors_become_an_answer_that_is_still_logged(toolbox):
    log = ListLog()
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    agent, _ = make_analyst(toolbox, anthropic.APIConnectionError(request=request), log=log)
    answer = agent.ask("Anything")
    assert answer.outcome == "error"
    assert "couldn't reach the Claude API" in answer.text
    assert log.records[0]["error"] == "couldn't reach the Claude API; check the internet connection"


# --- The question log ---------------------------------------------------------------------------


def test_the_file_log_writes_one_json_line_per_question(tmp_path):
    log = questionlog.FileLog(tmp_path / "logs" / "questions.jsonl")
    log.write({"question_id": "a", "question": "first"})
    where = log.write({"question_id": "b", "question": "second"})
    lines = (tmp_path / "logs" / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["question"] for line in lines] == ["first", "second"]
    assert where.endswith("(question b)")


def test_mongodb_failures_fall_back_to_the_file(tmp_path):
    class Unreachable:
        def insert_one(self, record):
            raise mongo_errors.ServerSelectionTimeoutError("no servers found")

    fallback = questionlog.FileLog(tmp_path / "questions.jsonl")
    where = questionlog.MongoLog(Unreachable(), fallback, "query_cf.questions").write(
        {"question_id": "a"}
    )
    assert "MongoDB failed: no servers found" in where
    assert json.loads(fallback.path.read_text(encoding="utf-8"))["question_id"] == "a"


def test_mongodb_is_used_only_when_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(questionlog.URI_VARIABLE, raising=False)
    log, problem = questionlog.open_log("query_cf", "questions", tmp_path / "q.jsonl")
    assert isinstance(log, questionlog.FileLog) and problem is None

    for uri, problem_text in [
        ("my-password", "doesn't start with mongodb+srv://"),
        ("mongodb://user:pw@cluster0:not-a-port/", "can't be used"),
    ]:
        monkeypatch.setenv(questionlog.URI_VARIABLE, uri)
        log, problem = questionlog.open_log("query_cf", "questions", tmp_path / "q.jsonl")
        assert isinstance(log, questionlog.FileLog) and problem_text in problem
        assert "my-password" not in problem and ":pw@" not in problem  # never print the secret

    monkeypatch.setenv(questionlog.URI_VARIABLE, "mongodb://127.0.0.1:1/")
    log, problem = questionlog.open_log("query_cf", "questions", tmp_path / "q.jsonl")
    assert isinstance(log, questionlog.MongoLog) and problem is None


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (mongo_errors.OperationFailure("bad auth", code=8000), "password was rejected"),
        (mongo_errors.ServerSelectionTimeoutError("timed out"), "IP access list"),
        (mongo_errors.InvalidURI("bad"), "isn't valid"),
    ],
)
def test_mongodb_problems_are_explained(error, message):
    assert message in questionlog.describe_mongo_error(error)


# --- The commands -----------------------------------------------------------------------------


@pytest.fixture
def cli(warehouse, tmp_path, monkeypatch):
    """run_pipeline.main against the planted warehouse, with a fake model and a local log."""
    monkeypatch.chdir(tmp_path)  # the log file is data/logs/questions.jsonl under here
    monkeypatch.setenv(analyst.API_KEY_VARIABLE, "test-key")
    monkeypatch.delenv(questionlog.URI_VARIABLE, raising=False)

    def run(*args, responses=()):
        client = FakeClient(*responses)
        monkeypatch.setattr(run_pipeline, "make_client", lambda: client)
        return run_pipeline.main(["--db", str(warehouse), *args]), client

    return run


def test_ask_prints_the_answer_its_sql_and_where_it_was_logged(cli, tmp_path, capsys):
    code, _ = cli(
        "ask",
        "How many complaints did Wells Fargo get, ever?",
        responses=[
            response(tool_call("query_metrics", WF_COUNT), stop="tool_use"),
            response(text("Wells Fargo received 3 complaints.")),
        ],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("Wells Fargo received 3 complaints.")
    assert "1. query_metrics: 1 row from 3 complaints" in out
    assert f"c.company_name = '{WF}'" in out
    assert "claude-haiku-5-5 answered in 2 requests" in out
    log_file = tmp_path / "data" / "logs" / "questions.jsonl"
    assert f"Logged to {Path('data') / 'logs' / 'questions.jsonl'}" in out
    assert json.loads(log_file.read_text(encoding="utf-8"))["answer"].startswith("Wells Fargo")


def test_ask_without_queries_says_so(cli, capsys):
    code, _ = cli("ask", "Which bank is worst?", responses=[response(text("Worst by what?"))])
    assert code == 0
    assert "No queries were run." in capsys.readouterr().out


def test_ask_needs_an_api_key(cli, monkeypatch, capsys):
    monkeypatch.delenv(analyst.API_KEY_VARIABLE)
    code, client = cli("ask", "Anything")
    assert code == 1
    assert "ANTHROPIC_API_KEY isn't set" in capsys.readouterr().out
    assert client.requests == []


def test_chat_keeps_the_conversation_for_follow_ups(cli, monkeypatch, capsys):
    questions = iter(["How many complaints did Wells Fargo get, ever?", "And Bank A?", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(questions))
    code, client = cli(
        "chat",
        responses=[
            response(tool_call("query_metrics", WF_COUNT), stop="tool_use"),
            response(text("Wells Fargo received 3 complaints.")),
            response(text("Bank A received 4.")),
        ],
    )
    assert code == 0
    assert "Bank A received 4." in capsys.readouterr().out
    follow_up = client.requests[2]["messages"]
    assert follow_up[0] == {
        "role": "user",
        "content": "How many complaints did Wells Fargo get, ever?",
    }
    assert follow_up[-1] == {"role": "user", "content": "And Bank A?"}


def test_check_accounts_reports_each_service(cli, monkeypatch, capsys):
    monkeypatch.setenv(questionlog.URI_VARIABLE, "mongodb+srv://user:pw@cluster0.example.net/")
    monkeypatch.setattr(questionlog, "check_mongo", lambda uri, database: None)
    code, _ = cli("check-accounts")
    out = capsys.readouterr().out
    assert code == 0
    assert "Claude API: OK, claude-haiku-5-5 (Claude Haiku 5.5)" in out
    assert "MongoDB: OK, the database user can write to query_cf" in out
    assert "Warehouse: OK, 7 complaints" in out

    def bad_password(uri, database):
        raise mongo_errors.OperationFailure("bad auth : authentication failed", code=8000)

    monkeypatch.setattr(questionlog, "check_mongo", bad_password)
    code, _ = cli("check-accounts")
    assert code == 1
    assert "password was rejected" in capsys.readouterr().out
