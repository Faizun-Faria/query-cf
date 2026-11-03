"""Phase 5: run the evaluation questions through the analyst and score the answers (US-12).

Each answer is scored by code against `expected_answer` in evals/questions.csv, so the same
answer always gets the same score (D-040):

    metric, sql     every expected figure appears in the answer (7,058; 33.3%), every expected
                    name or period appears too (matched on its first long word, so "TransUnion"
                    counts for TRANSUNION INTERMEDIATE HOLDINGS, INC.), and at least one query ran
    clarify         no tool was called, and the answer asks exactly one question
    cannot_answer   no tool was called, and the answer offers a metric the expected answer names

Figures must be written the way the rules say (R-17): a rate of 33.25% only matches 33.3%.
The results report lists every failure with the answer, so a scoring mistake can be seen.
"""

from __future__ import annotations

import calendar
import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from query_cf.semantic import SemanticLayer

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_FILE = ROOT / "evals" / "questions.csv"
REPORT_FILE = ROOT / "docs" / "12-evaluation-results.md"
COLUMNS = (
    "id",
    "persona",
    "difficulty",
    "kind",
    "question",
    "expected_answer",
    "how_computed",
    "notes",
)
KINDS = ("metric", "sql", "clarify", "cannot_answer")
DIFFICULTIES = ("easy", "medium", "hard", "n/a")
RANKING_WORDS = {"highest", "lowest"}
# Words in many company names, which say nothing about which company an answer names.
GENERIC_WORDS = {
    "bank",
    "financial",
    "company",
    "holding",
    "holdings",
    "services",
    "national",
    "association",
    "corporation",
    "group",
}

# A figure as answers write it: 7,058 or 7058, 33.3% or 33.3 percent.
FIGURE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(\s?%|\s?percent\b)?", re.I)
PERIOD = re.compile(r"(\d{4})(?:-(\d{2}))?")


class EvaluationError(ValueError):
    """The questions file can't be used; the message names the row."""


@dataclass(frozen=True)
class Question:
    id: str
    persona: str
    difficulty: str
    kind: str
    question: str
    expected_answer: str
    how_computed: str
    notes: str


def load_questions(path: Path = QUESTIONS_FILE) -> list[Question]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = tuple(next(reader, ()))
        if header != COLUMNS:
            raise EvaluationError(f"{path}: the header must be {','.join(COLUMNS)}")
        rows = [row for row in reader if row]
    questions, seen = [], set()
    for number, row in enumerate(rows, start=2):
        if len(row) != len(COLUMNS):
            raise EvaluationError(f"{path} line {number}: {len(row)} columns, not {len(COLUMNS)}")
        q = Question(*(value.strip() for value in row))
        if q.id in seen:
            raise EvaluationError(f"{path}: {q.id} is listed twice")
        if q.kind not in KINDS:
            raise EvaluationError(f"{path}: {q.id} has kind {q.kind!r}; use {', '.join(KINDS)}")
        if q.difficulty not in DIFFICULTIES:
            raise EvaluationError(f"{path}: {q.id} has difficulty {q.difficulty!r}")
        if not q.expected_answer:
            raise EvaluationError(f"{path}: {q.id} has no expected_answer")
        seen.add(q.id)
        questions.append(q)
    return questions


# --- What an answer must contain -------------------------------------------------------------


def _figure_key(number: str, decimals: str | None, percent: str | None) -> str:
    return number.replace(",", "") + (decimals or "") + ("%" if percent else "")


def figures_in(text: str) -> set[str]:
    """Every figure in a text, written without thousands separators: 7058, 33.3%."""
    return {_figure_key(*match.groups()) for match in FIGURE.finditer(text)}


def expected_items(question: Question) -> list[tuple[tuple[str, ...], str]]:
    """The (label parts, figure) pairs of a metric or sql expected answer.

    "2022=31.9%; 2023=36.5%" gives [(("2022",), "31.9%"), (("2023",), "36.5%")], and
    "highest / BARCLAYS BANK DELAWARE=100.0%" gives (("highest", "BARCLAYS BANK DELAWARE"), ...).
    A single figure such as "7,058" has no label.
    """
    items = []
    for part in question.expected_answer.split("; "):
        label, sep, value = part.rpartition("=")
        labels = tuple(p.strip() for p in label.split(" / ")) if sep else ()
        items.append((labels, value.strip()))
    return items


def label_patterns(label: str) -> list[re.Pattern] | None:
    """Ways an answer may write a label, or None for words that need no check.

    A year must appear as written; a month as 2026-01, January 2026 or Jan 2026; a name by
    its first word of four or more letters that isn't a generic word such as "bank", as a
    whole word in any case ("TD BANK US HOLDING COMPANY" has none, so "TD" is used).
    """
    if label.lower() in RANKING_WORDS:
        return None
    period = PERIOD.fullmatch(label)
    if period:
        year, month = period.groups()
        if not month:
            return [re.compile(rf"\b{year}\b")]
        names = (calendar.month_name[int(month)], calendar.month_abbr[int(month)])
        return [re.compile(rf"\b{year}-{month}\b")] + [
            re.compile(rf"\b{name}\.?\s+{year}\b", re.I) for name in names
        ]
    words = [w.strip(".,&()'\"") for w in label.split()]
    distinctive = (
        w for w in words if sum(c.isalpha() for c in w) >= 4 and w.lower() not in GENERIC_WORDS
    )
    key = next(distinctive, words[0] if words else "")
    return [re.compile(rf"(?<!\w){re.escape(key)}(?!\w)", re.I)] if key else None


def offered_metrics(question: Question, layer: SemanticLayer) -> list[str]:
    """The defined metrics a cannot_answer expected answer says the analyst should offer.

    Only labels of two or more words count: "Complaints" would match almost any answer.
    """
    text = question.expected_answer.lower()
    labels = [m.label for m in layer.metrics.values() if len(m.label.split()) > 1]
    return [label for label in labels if label.lower() in text]


# --- Scoring ---------------------------------------------------------------------------------


@dataclass
class Score:
    passed: bool
    misses: list[str] = field(default_factory=list)


def score(question: Question, answer, layer: SemanticLayer) -> Score:
    """Score one answer from the analyst (anything with text, outcome and steps)."""
    if answer.outcome != "answered":
        return Score(False, [f"the analyst didn't answer ({answer.outcome}): {answer.text}"])
    text, steps = answer.text, answer.steps

    if question.kind == "clarify":
        misses = []
        if steps:
            misses.append(f"called {len(steps)} tool(s) before asking")
        asked = text.count("?")
        if asked != 1:
            misses.append(f"asked {asked} questions instead of exactly one")
        return Score(not misses, misses)

    if question.kind == "cannot_answer":
        misses = []
        if steps:
            misses.append(f"called {len(steps)} tool(s) for a question it can't answer")
        offers = offered_metrics(question, layer)
        if not any(label.lower() in text.lower() for label in offers):
            misses.append(f"offered none of: {', '.join(offers) or 'no metric named'}")
        return Score(not misses, misses)

    misses = []
    if not any(step.get("ok") for step in steps):
        misses.append("answered without running a query")
    found = figures_in(text)
    for labels, value in expected_items(question):
        key = next(iter(figures_in(value)), value)
        name = " / ".join(labels)
        if key not in found:
            misses.append(f"missing {value}" + (f" for {name}" if name else ""))
        for label in labels:
            patterns = label_patterns(label)
            if patterns and not any(p.search(text) for p in patterns):
                misses.append(f"missing {label}")
    return Score(not misses, list(dict.fromkeys(misses)))


# --- Running and reporting --------------------------------------------------------------------


@dataclass
class Result:
    question: Question
    answer: object
    score: Score


def run(questions: list[Question], analyst, layer: SemanticLayer, progress=print) -> list[Result]:
    """Ask each question in a new conversation and score the answer."""
    results = []
    for q in questions:
        analyst.new_conversation()
        answer = analyst.ask(q.question, tags={"evaluation": q.id})
        result = Result(q, answer, score(q, answer, layer))
        results.append(result)
        progress(
            f"{q.id}  {q.difficulty:<6} {q.kind:<13} {'PASS' if result.score.passed else 'FAIL'}"
            f"  {len(answer.steps)} tool calls  ${answer.cost_usd:.4f}"
        )
    return results


def _rate(results: list[Result]) -> str:
    passed = sum(r.score.passed for r in results)
    return f"{passed} of {len(results)} ({100 * passed / len(results):.0f}%)"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render_report(results: list[Result], settings, prompt_version: str, when: datetime) -> str:
    """The results report as Markdown, written by `python run_pipeline.py evaluate`."""
    cost = sum(r.answer.cost_usd for r in results)
    seconds = sum(r.answer.seconds for r in results)
    tokens = Counter()
    for r in results:
        tokens.update(r.answer.usage)
    prompt = sum(
        tokens[k]
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    out = [
        "# Evaluation results",
        "",
        "Generated by `python run_pipeline.py evaluate` from "
        "[evals/questions.csv](../evals/questions.csv). Don't edit by hand: re-run it. How "
        "answers are scored is in the [evaluation design](11-evaluation.md).",
        "",
        "| Run | Value |",
        "| --- | --- |",
        f"| Date | {when:%Y-%m-%d %H:%M} UTC |",
        f"| Model | `{settings.model}` |",
        f"| Effort | `{settings.effort}` |",
        f"| Prompt version | `{prompt_version}` |",
        f"| Questions | {len(results)} |",
        f"| Passed | {_rate(results)} |",
        f"| Cost of the run | ${cost:.4f} |",
        f"| Cost per 100 questions | ${100 * cost / len(results):.2f} |",
        f"| Tokens | {prompt:,} input ({tokens['cache_read_input_tokens']:,} from the cache), "
        f"{tokens['output_tokens']:,} output |",
        f"| Mean time per question | {seconds / len(results):.1f} seconds |",
    ]

    for title, key, order in (
        ("By difficulty", lambda r: r.question.difficulty, DIFFICULTIES),
        ("By kind", lambda r: r.question.kind, KINDS),
    ):
        out += ["", f"## {title}", "", "| Group | Passed |", "| --- | --- |"]
        for group in order:
            members = [r for r in results if key(r) == group]
            if members:
                out.append(f"| {group} | {_rate(members)} |")

    out += [
        "",
        "## Every question",
        "",
        "| Question | Difficulty | Kind | Result | Tool calls | Cost |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        verdict = "pass" if r.score.passed else "**fail**"
        out.append(
            f"| {r.question.id} | {r.question.difficulty} | {r.question.kind} | {verdict} "
            f"| {len(r.answer.steps)} | ${r.answer.cost_usd:.4f} |"
        )

    failures = [r for r in results if not r.score.passed]
    out += ["", "## Failures", ""]
    if not failures:
        out.append("None.")
    for r in failures:
        out += [
            f"### {r.question.id}: {_cell(r.question.question)}",
            "",
            f"- **Expected:** {_cell(r.question.expected_answer)}",
            f"- **What was wrong:** {_cell('; '.join(r.score.misses))}",
            f"- **Tools called:** {', '.join(s['tool'] for s in r.answer.steps) or 'none'}",
            "",
            "Answer:",
            "",
            *[f"> {line}" if line else ">" for line in r.answer.text.splitlines()],
            "",
        ]
    return "\n".join(out).rstrip() + "\n"
