"""Phase 5: scoring the analyst's answers against the answer key (US-12).

The scoring is tested on the real question bank with answers written by hand, and the
evaluate command runs against the planted warehouse with the fake model from
test_analyst.py, so nothing here calls the Claude API.
"""

import csv
import json
import re
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_analyst import WF_COUNT, FakeClient, response, text, tool_call, warehouse  # noqa: F401

import run_pipeline
from query_cf import analyst, evaluate, questionlog
from query_cf.analyst import load_settings
from query_cf.evaluate import EvaluationError, load_questions, score
from query_cf.semantic import load_layer

LAYER = load_layer()
QUESTIONS = {q.id: q for q in load_questions()}
RAN_A_QUERY = [{"tool": "query_metrics", "ok": True}]


def answer(words, steps=RAN_A_QUERY, outcome="answered"):
    return SimpleNamespace(text=words, steps=steps, outcome=outcome)


def misses(question_id, words, steps=RAN_A_QUERY, outcome="answered"):
    return score(QUESTIONS[question_id], answer(words, steps, outcome), LAYER).misses


# --- The question bank ------------------------------------------------------------------------


def test_the_question_bank_loads_with_an_answer_for_every_question():
    assert len(QUESTIONS) == 25
    assert Counter(q.kind for q in QUESTIONS.values()) == {
        "metric": 20,
        "sql": 2,
        "clarify": 1,
        "cannot_answer": 2,
    }
    for q in QUESTIONS.values():
        if q.kind in ("metric", "sql"):
            assert all(evaluate.figures_in(value) for _, value in evaluate.expected_items(q)), q.id
        if q.kind == "cannot_answer":
            assert evaluate.offered_metrics(q, LAYER), q.id


def test_a_perfect_answer_to_every_question_passes():
    for q in QUESTIONS.values():
        if q.kind in ("metric", "sql"):
            rows = [
                f"| {' / '.join(labels)} | {value} |"
                for labels, value in evaluate.expected_items(q)
            ]
            words = "| Group | Figure |\n| --- | --- |\n" + "\n".join(rows)
        elif q.kind == "clarify":
            words = "Do you mean the most complaints, or the lowest timely response rate?"
        else:
            words = f"The data can't say. I can give the {evaluate.offered_metrics(q, LAYER)[0]}."
        steps = RAN_A_QUERY if q.kind in ("metric", "sql") else []
        assert score(q, answer(words, steps), LAYER).passed, q.id


@pytest.mark.parametrize(
    "header_or_row",
    [
        "id,persona,difficulty,kind,question,expected_answer,notes",
        "Q01,ops_lead,easy,metric,How many?,7,how,notes",
        "Q02,ops_lead,easy,guess,How many?,7,how,notes",
        "Q02,ops_lead,trivial,metric,How many?,7,how,notes",
        "Q02,ops_lead,easy,metric,How many?,,how,notes",
    ],
)
def test_the_questions_file_is_checked(tmp_path, header_or_row):
    good = "Q01,ops_lead,easy,metric,How many?,7,how,notes"
    header = ",".join(evaluate.COLUMNS)
    lines = (
        [header_or_row, good] if header_or_row.startswith("id,") else [header, good, header_or_row]
    )
    path = tmp_path / "questions.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(EvaluationError):
        load_questions(path)


# --- Scoring metric and sql answers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("question_id", "words"),
    [
        ("Q01", "Capital One received 7,058 credit card complaints in 2024."),
        ("Q01", "Capital One received 7058 credit card complaints in 2024."),
        ("Q02", "2022: 31.9%, 2023: 36.5 %, 2024: 39.0 percent and 2025: 33.3%."),
        (
            "Q22",
            "January 2026: 9,516; Feb 2026: 7,401; March 2026: 8,325; April 2026: 8,229; "
            "May 2026: 7,919; June 2026: 7,453; July 2026: 8,027; 2026-08: 7,996.",
        ),
    ],
)
def test_figures_and_periods_count_however_they_are_written(question_id, words):
    assert misses(question_id, words) == []


def test_figures_must_match_to_the_decimal_place_the_rules_set():
    words = "2022: 31.86%, 2023: 36.48%, 2024: 39.03% and 2025: 33.25%."
    assert misses("Q02", words) == [
        "missing 31.9% for 2022",
        "missing 36.5% for 2023",
        "missing 39.0% for 2024",
        "missing 33.3% for 2025",
    ]
    assert misses("Q01", "Capital One received about 7,100.") == ["missing 7,058"]


def test_every_expected_row_must_be_in_the_answer():
    words = "2022: 31.9%, 2023: 36.5% and 2024: 39.0%."
    assert misses("Q02", words) == ["missing 33.3% for 2025", "missing 2025"]


def test_names_are_matched_on_their_first_long_word():
    rows = [
        ("TransUnion", "4,785,659"),
        ("Equifax", "4,658,351"),
        ("Experian", "4,222,327"),
        ("Bank of America", "186,233"),
        ("JPMorgan Chase", "175,270"),
        ("Wells Fargo", "174,517"),
        ("Capital One", "171,222"),
        ("Citibank", "141,213"),
        ("Synchrony", "83,488"),
        ("Resurgent Capital Services", "71,113"),
    ]
    words = "\n".join(f"{n}. {name}: {count}" for n, (name, count) in enumerate(rows, 1))
    assert misses("Q06", words) == []
    without_synchrony = words.replace("Synchrony", "Another lender")
    assert misses("Q06", without_synchrony) == ["missing SYNCHRONY FINANCIAL"]


def test_every_company_tied_at_the_cut_off_must_be_listed():
    words = (
        "Highest, all at 100.0%: Barclays, Bread Financial, Citibank, Discover and Equifax. "
        "Lowest: American Express 98.9%, CLGF Holdco 99.5%, Bank of America 99.5%, "
        "Synchrony 99.6% and Capital One 100.0%."
    )
    missing = misses("Q12", words)
    assert "missing Experian Information Solutions Inc." in missing
    assert "missing WELLS FARGO & COMPANY" in missing
    assert len(missing) == 8  # the eight tied companies left out


def test_a_figure_without_a_query_behind_it_fails():
    assert misses("Q01", "7,058", steps=[]) == ["answered without running a query"]
    blocked = [{"tool": "run_sql", "ok": False}]
    assert misses("Q01", "7,058", steps=blocked) == ["answered without running a query"]


def test_an_answer_that_never_came_fails():
    [reason] = misses("Q01", "The Claude API returned an error", outcome="error")
    assert reason.startswith("the analyst didn't answer (error)")


# --- Scoring clarify and cannot_answer answers -------------------------------------------------


def test_a_clarifying_answer_asks_exactly_one_question_and_runs_nothing():
    one = "Do you mean the most complaints, the lowest timely response rate or relief rate?"
    assert misses("Q04", one, steps=[]) == []
    two = "Which measure do you mean? And for which period?"
    assert misses("Q04", two, steps=[]) == ["asked 2 questions instead of exactly one"]
    assert misses("Q04", one) == ["called 1 tool(s) before asking"]


def test_a_cannot_answer_reply_offers_a_defined_metric_and_runs_nothing():
    offer = "The data doesn't record amounts. I can give the monetary relief rate instead."
    assert misses("Q05", offer, steps=[]) == []
    assert misses("Q05", offer) == ["called 1 tool(s) for a question it can't answer"]
    [reason] = misses("Q05", "The data doesn't record amounts.", steps=[])
    assert reason.startswith("offered none of: Relief rate, Monetary relief rate")
    assert misses("Q11", "No resolution dates; the timely response rate is close.", steps=[]) == []


# --- The report -----------------------------------------------------------------------------------


def fake_result(question_id, passed, cost=0.002):
    usage = {
        "requests": 2,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 900,
    }
    reply = SimpleNamespace(
        text="Line one.\n\nLine two.", steps=RAN_A_QUERY, usage=usage, cost_usd=cost, seconds=3.0
    )
    verdict = evaluate.Score(passed, [] if passed else ["missing 7,058"])
    return evaluate.Result(QUESTIONS[question_id], reply, verdict)


def test_the_report_shows_accuracy_by_difficulty_and_kind_and_each_failure():
    results = [fake_result("Q01", True), fake_result("Q02", False), fake_result("Q04", True)]
    when = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    report = evaluate.render_report(results, load_settings(), "abc123def456", when)
    assert "| Model | `claude-haiku-5-5` |" in report
    assert "| Passed | 2 of 3 (67%) |" in report
    assert "| Cost per 100 questions | $0.20 |" in report
    assert "| Tokens | 3,000 input (2,700 from the cache), 150 output |" in report
    assert "| easy | 1 of 1 (100%) |" in report and "| medium | 0 of 1 (0%) |" in report
    assert "| clarify | 1 of 1 (100%) |" in report
    assert "| Q02 | medium | metric | **fail** | 1 | $0.0020 |" in report
    assert "### Q02: How did the relief rate" in report
    assert "- **What was wrong:** missing 7,058" in report
    assert "> Line one.\n>\n> Line two." in report


def test_the_committed_results_match_the_pinned_model():
    """A model or effort change needs a new evaluation run before it ships."""
    if not run_pipeline.REPORT.exists():
        pytest.skip("no evaluation has been run yet")
    report = run_pipeline.REPORT.read_text(encoding="utf-8")
    settings = load_settings()
    assert re.search(rf"\| Model \| `{re.escape(settings.model)}` \|", report), "run: evaluate"
    assert re.search(rf"\| Effort \| `{settings.effort}` \|", report), "run: evaluate"


# --- The evaluate command -------------------------------------------------------------------------


@pytest.fixture
def evaluate_cli(warehouse, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(analyst.API_KEY_VARIABLE, "test-key")
    monkeypatch.delenv(questionlog.URI_VARIABLE, raising=False)
    questions = tmp_path / "questions.csv"
    with open(questions, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(evaluate.COLUMNS)
        writer.writerow(
            ["Q01", "ops_lead", "easy", "metric", "Wells Fargo complaints, ever?", "3", "", ""]
        )
        writer.writerow(
            ["Q02", "ops_lead", "n/a", "clarify", "Which bank is worst?", "CLARIFY: asks", "", ""]
        )

    def run(*args, responses=()):
        client = FakeClient(*responses)
        monkeypatch.setattr(run_pipeline, "make_client", lambda: client)
        command = ["--db", str(warehouse), "evaluate", "--questions", str(questions), *args]
        return run_pipeline.main(command), client

    return run


ANSWERS = [
    response(tool_call("query_metrics", WF_COUNT), stop="tool_use"),
    response(text("Wells Fargo received 3 complaints.")),
    response(text("Worst by number of complaints or by timely response rate?")),
]


def test_evaluate_scores_every_question_and_writes_the_report(evaluate_cli, tmp_path, capsys):
    report = tmp_path / "results.md"
    code, client = evaluate_cli("--report", str(report), responses=ANSWERS)
    out = capsys.readouterr().out
    assert code == 0
    assert "Q01  easy   metric        PASS  1 tool calls" in out
    assert "Passed 2 of 2." in out
    assert "| Passed | 2 of 2 (100%) |" in report.read_text(encoding="utf-8")
    # Each question starts a new conversation.
    assert len(client.requests[2]["messages"]) == 1
    records = [
        json.loads(line)
        for line in (tmp_path / "data" / "logs" / "questions.jsonl").read_text("utf-8").splitlines()
    ]
    assert [r["tags"] for r in records] == [{"evaluation": "Q01"}, {"evaluation": "Q02"}]


def test_evaluate_can_run_some_questions_without_rewriting_the_report(evaluate_cli, capsys):
    code, client = evaluate_cli("--only", "Q02", responses=ANSWERS[2:])
    out = capsys.readouterr().out
    assert code == 0
    assert "Passed 1 of 1." in out and "the report wasn't rewritten" in out
    assert len(client.requests) == 1


def test_evaluate_rejects_unknown_question_ids(evaluate_cli, capsys):
    code, client = evaluate_cli("--only", "Q99")
    assert code == 2
    assert "Not in questions.csv: Q99" in capsys.readouterr().out
    assert client.requests == []


def test_an_api_failure_writes_no_report(evaluate_cli, tmp_path, capsys):
    import anthropic
    import httpx2

    failure = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    report = tmp_path / "results.md"
    code, _ = evaluate_cli("--report", str(report), responses=[failure, ANSWERS[2]])
    assert code == 1
    assert "The Claude API failed on Q01, so no report was written." in capsys.readouterr().out
    assert not report.exists()
