"""Phase 5: run the evaluation questions through the analyst and score the answers (US-12).

Each answer is scored by code against `expected_answer` in evals/questions.csv, so the same
answer always gets the same score (D-040):

    metric, sql     every expected figure appears in the answer (7,058; 33.3%), every expected
                    name or period appears too (matched on its first long word, so "TransUnion"
                    counts for TRANSUNION INTERMEDIATE HOLDINGS, INC.), and at least one query ran
    clarify         no tool was called, and the answer asks exactly one question
    cannot_answer   no tool was called, and the answer offers a metric the expected answer names

Figures must be written the way the rules say (R-17): a rate of 33.25% only matches 33.3%;
a count may also be written in words ("Eighteen"). Separately from the score, every answer
is checked for figures that no tool returned (R-16), such as a total the model added up
itself. Each question can be asked several times, because the model's answers vary.
"""

from __future__ import annotations

import calendar
import csv
import json
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
# A count written as words, up to ninety-nine: "Eighteen", "twenty-one".
UNITS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
NUMBER_WORD = re.compile(
    rf"\b(?:({'|'.join(TENS)})(?:[- ]({'|'.join(UNITS[1:10])}))?|({'|'.join(UNITS)}))\b", re.I
)


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


# --- Reading figures -------------------------------------------------------------------------


def _figure_key(number: str, decimals: str | None, percent: str | None) -> str:
    return number.replace(",", "") + (decimals or "") + ("%" if percent else "")


def _word_value(match: re.Match) -> str:
    tens, unit, alone = match.groups()
    if alone:
        return str(UNITS.index(alone.lower()))
    return str(20 + 10 * TENS.index(tens.lower()) + (UNITS.index(unit.lower()) if unit else 0))


def figures_in(text: str, words: bool = False) -> set[str]:
    """Every figure in a text, written without thousands separators: 7058, 33.3%.

    With words, counts written as words up to ninety-nine count too: "Eighteen" is 18.
    """
    found = {_figure_key(*match.groups()) for match in FIGURE.finditer(text)}
    if words:
        found |= {_word_value(match) for match in NUMBER_WORD.finditer(text)}
    return found


def numbers_in(text: str) -> set[str]:
    """Every number in a text, without thousands separators or a % sign: 7058, 33.3."""
    return {key.rstrip("%") for key in figures_in(text)}


def _reports_data(match: re.Match) -> bool:
    """A figure that reports data: a rate, a decimal, or a count of 100 or more.

    Small whole numbers and years are left out: answers use them for dates, ranks and periods.
    """
    number, decimals, percent = match.groups()
    if percent or decimals or "," in number:
        return True
    return int(number) >= 100 and not 1900 <= int(number) <= 2100


def unsupported_figures(answer, question, known: set[str] = frozenset()) -> list[str]:
    """Figures in an answer that no tool returned and that the question and prompt don't hold.

    These are usually the model's own arithmetic, such as a total of the rows it was given,
    which R-16 asks it not to do. `known` holds the numbers in the system prompt.
    """
    known = set(known) | numbers_in(question.question)
    for step in answer.steps:
        seen = {k: step.get(k) for k in ("input", "result", "error")}
        known |= numbers_in(json.dumps(seen, ensure_ascii=False, default=str))
    flagged = []
    for match in FIGURE.finditer(answer.text):
        if _reports_data(match) and _figure_key(*match.groups()).rstrip("%") not in known:
            flagged.append(match.group(0).strip())
    return list(dict.fromkeys(flagged))


# --- What an answer must contain -------------------------------------------------------------


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
    unsupported: list[str] = field(default_factory=list)  # reported, but doesn't fail a question


def _misses(question: Question, answer, layer: SemanticLayer) -> list[str]:
    """What the answer is missing; empty when it passes."""
    if answer.outcome != "answered":
        return [f"the analyst didn't answer ({answer.outcome}): {answer.text}"]
    text, steps, misses = answer.text, answer.steps, []

    if question.kind == "clarify":
        if steps:
            misses.append(f"called {len(steps)} tool(s) before asking")
        asked = text.count("?")
        if asked != 1:
            misses.append(f"asked {asked} questions instead of exactly one")
        return misses

    if question.kind == "cannot_answer":
        if steps:
            misses.append(f"called {len(steps)} tool(s) for a question it can't answer")
        offers = offered_metrics(question, layer)
        if not any(label.lower() in text.lower() for label in offers):
            misses.append(f"offered none of: {', '.join(offers) or 'no metric named'}")
        return misses

    if not any(step.get("ok") for step in steps):
        misses.append("answered without running a query")
    found = figures_in(text, words=True)
    for labels, value in expected_items(question):
        key = next(iter(figures_in(value)), value)
        name = " / ".join(labels)
        if key not in found:
            misses.append(f"missing {value}" + (f" for {name}" if name else ""))
        for label in labels:
            patterns = label_patterns(label)
            if patterns and not any(p.search(text) for p in patterns):
                misses.append(f"missing {label}")
    return list(dict.fromkeys(misses))


def score(question: Question, answer, layer: SemanticLayer, known: set[str] = frozenset()) -> Score:
    """Score one answer from the analyst (anything with text, outcome and steps)."""
    misses = _misses(question, answer, layer)
    return Score(not misses, misses, unsupported_figures(answer, question, known))


# --- Running ---------------------------------------------------------------------------------


@dataclass
class Result:
    question: Question
    answer: object
    score: Score
    run: int = 1


def run(
    questions: list[Question], analyst, layer: SemanticLayer, runs: int = 1, progress=print
) -> list[Result]:
    """Ask each question in a new conversation, `runs` times over, and score every answer."""
    known = numbers_in(analyst.system_prompt)
    results = []
    for number in range(1, runs + 1):
        for q in questions:
            analyst.new_conversation()
            answer = analyst.ask(q.question, tags={"evaluation": q.id, "run": number})
            result = Result(q, answer, score(q, answer, layer, known), number)
            results.append(result)
            which = f"{q.id} run {number}" if runs > 1 else q.id
            flagged = len(result.score.unsupported)
            progress(
                f"{which}  {q.difficulty:<6} {q.kind:<13} "
                f"{'PASS' if result.score.passed else 'FAIL'}  {len(answer.steps)} tool calls  "
                f"${answer.cost_usd:.4f}"
                + (f"  {flagged} figure(s) no tool returned" if flagged else "")
            )
    return results


# --- Reporting -------------------------------------------------------------------------------


def _share(count: int, total: int, unit: str = "") -> str:
    return f"{count} of {total}{unit} ({100 * count / total:.0f}%)"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def by_question(results: list[Result]) -> dict[str, list[Result]]:
    grouped: dict[str, list[Result]] = {}
    for r in results:
        grouped.setdefault(r.question.id, []).append(r)
    return grouped


def render_report(results: list[Result], settings, prompt_version: str, when: datetime) -> str:
    """The results report as Markdown, written by `python run_pipeline.py evaluate`."""
    grouped = by_question(results)
    runs = max(r.run for r in results)
    every_run = {qid for qid, rs in grouped.items() if all(r.score.passed for r in rs)}
    passed = sum(r.score.passed for r in results)
    flagged = [r for r in results if r.score.unsupported]
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
        f"| Questions | {len(grouped)} |",
        f"| Runs of each question | {runs} |",
        f"| Passed every run | {_share(len(every_run), len(grouped))} |",
        f"| Pass rate | {_share(passed, len(results), ' answers')} |",
        f"| Answers with figures no tool returned | {_share(len(flagged), len(results))} |",
        f"| Cost of the run | ${cost:.4f} |",
        f"| Cost per 100 questions | ${100 * cost / len(results):.2f} |",
        f"| Tokens | {prompt:,} input ({tokens['cache_read_input_tokens']:,} from the cache), "
        f"{tokens['output_tokens']:,} output |",
        f"| Mean time per answer | {seconds / len(results):.1f} seconds |",
    ]

    for title, key, order in (
        ("By difficulty", lambda q: q.difficulty, DIFFICULTIES),
        ("By kind", lambda q: q.kind, KINDS),
    ):
        out += [
            "",
            f"## {title}",
            "",
            "| Group | Passed every run | Pass rate |",
            "| --- | --- | --- |",
        ]
        for group in order:
            ids = [qid for qid, rs in grouped.items() if key(rs[0].question) == group]
            if ids:
                answers = [r for qid in ids for r in grouped[qid]]
                good = sum(r.score.passed for r in answers)
                out.append(
                    f"| {group} | {_share(sum(q in every_run for q in ids), len(ids))} "
                    f"| {_share(good, len(answers))} |"
                )

    out += [
        "",
        "## Every question",
        "",
        "| Question | Difficulty | Kind | Passed | Tool calls | Cost |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for qid, rs in grouped.items():
        q, good = rs[0].question, sum(r.score.passed for r in rs)
        verdict = f"{good} of {len(rs)}" if good == len(rs) else f"**{good} of {len(rs)}**"
        calls = ", ".join(str(len(r.answer.steps)) for r in rs)
        out.append(
            f"| {qid} | {q.difficulty} | {q.kind} | {verdict} | {calls} "
            f"| ${sum(r.answer.cost_usd for r in rs):.4f} |"
        )

    out += ["", "## Failures", ""]
    failing = {qid: rs for qid, rs in grouped.items() if qid not in every_run}
    if not failing:
        out += ["None.", ""]
    for qid, rs in failing.items():
        q = rs[0].question
        failed = [r for r in rs if not r.score.passed]
        out += [
            f"### {qid}: {_cell(q.question)}",
            "",
            f"Failed {len(failed)} of {len(rs)} runs. **Expected:** {_cell(q.expected_answer)}",
        ]
        for r in failed:
            tools = ", ".join(s["tool"] for s in r.answer.steps) or "none"
            out += [
                "",
                f"**Run {r.run}:** {_cell('; '.join(r.score.misses))}. Tools called: {tools}.",
                "",
                *[f"> {line}" if line else ">" for line in r.answer.text.splitlines()],
            ]
        out.append("")

    out += [
        "## Figures no tool returned",
        "",
        "Figures in an answer that no tool result, the question or the system prompt holds: "
        "usually the model's own arithmetic, which rule R-16 asks it not to do. They don't fail "
        "a question.",
        "",
    ]
    if not flagged:
        out.append("None.")
    for r in flagged:
        out.append(f"- {r.question.id}, run {r.run}: {', '.join(r.score.unsupported)}")
    return "\n".join(out).rstrip() + "\n"
