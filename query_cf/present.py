"""Phase 6: how an answer is shown, chosen from the shape of its query results (US-13).

Code chooses, not the model, so the same answer always looks the same (D-051):

    no query ran                      text only: a clarifying question, or one it can't answer
    one query, one row, one figure    a number
    one query grouped by date         a line chart
    one query grouped by a category   a bar chart, ranked
    several queries                   a report: the text, then each query's number or chart

Charts are drawn from the raw values the tools return next to the formatted rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # plotly is imported only when a chart is drawn, so commands stay quick
    import plotly.graph_objects as go

# Columns that hold a date or a period, from query_metrics (received_year) or run_sql (year).
TIME_COLUMN = re.compile(r"(^|_)(day|week|month|quarter|year)$")
LOOKUPS = {"list_values"}


@dataclass
class Part:
    """One query's result as it is shown: a number, a chart, or a table."""

    kind: str  # number, line, bar or table
    title: str
    value: str = ""  # for a number
    figure: go.Figure | None = None  # for a chart


@dataclass
class Presentation:
    kind: str  # text, number, chart or report
    parts: list[Part] = field(default_factory=list)


def _label(column: str) -> str:
    return column.replace("_", " ").capitalize()


def _is_figure(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def part_for(result: dict) -> Part:
    """How one query result is shown."""
    import plotly.graph_objects as go

    columns, values, rows = result["columns"], result["values"], result["rows"]
    percent = set(result.get("percent_columns", []))
    # A year from run_sql is a number, but it labels the rows rather than measuring them.
    figures = [
        i
        for i, c in enumerate(columns)
        if values and not TIME_COLUMN.search(c) and all(_is_figure(r[i]) for r in values)
    ]
    labels = [i for i in range(len(columns)) if i not in figures]
    if not values or not figures:
        return Part("table", "Result")

    measure = figures[0]
    title = _label(columns[measure])
    if len(values) == 1 and not labels:
        return Part("number", title, value=rows[0][measure])

    # Labels come from the formatted rows, so a year reads 2016 and a month 2026-01.
    x = [" / ".join(str(row[i]) for i in labels) for row in rows] if labels else []
    y = [row[measure] for row in values]
    time = len(labels) == 1 and TIME_COLUMN.search(columns[labels[0]])
    if time and len(values) > 1:
        figure = go.Figure(go.Scatter(x=x, y=y, mode="lines+markers", name=title))
        figure.update_layout(xaxis_title=_label(columns[labels[0]]), yaxis_title=title)
        if columns[measure] in percent:
            figure.update_layout(yaxis_ticksuffix="%")
        kind = "line"
    else:
        # Largest at the top: a horizontal bar chart lists its first bar at the bottom.
        figure = go.Figure(go.Bar(x=y[::-1], y=x[::-1], orientation="h", name=title))
        figure.update_layout(xaxis_title=title, height=max(260, 34 * len(values) + 120))
        if columns[measure] in percent:
            figure.update_layout(xaxis_ticksuffix="%")
        kind = "bar"
    figure.update_layout(margin={"l": 10, "r": 10, "t": 30, "b": 10}, showlegend=False)
    return Part(kind, title, figure=figure)


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def describe_step(step: dict) -> tuple[str, str]:
    """One tool call in words, and the SQL it ran (or tried to run), for the working shown."""
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
    sql = step.get("sql") or (step["input"].get("sql", "") if tool == "run_sql" else "")
    return summary, sql


def usage_line(answer) -> str:
    usage = answer.usage
    prompt = sum(
        usage[k] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return (
        f"{answer.model_version or 'No model'} answered in {plural(usage['requests'], 'request')}: "
        f"{prompt:,} input tokens ({usage['cache_read_input_tokens']:,} from the cache) and "
        f"{usage['output_tokens']:,} output tokens, ${answer.cost_usd:.4f}"
    )


def present(answer) -> Presentation:
    """How an answer from the analyst is shown, from the queries that ran."""
    if answer.outcome != "answered":
        return Presentation("text")
    queries = [
        step["result"]
        for step in answer.steps
        if step.get("ok")
        and step["tool"] not in LOOKUPS
        and step.get("result", {}).get("values") is not None
    ]
    if not queries:
        return Presentation("text")
    parts = [part_for(result) for result in queries]
    if len(parts) > 1:
        return Presentation("report", parts)
    kind = {"number": "number", "line": "chart", "bar": "chart"}.get(parts[0].kind, "text")
    return Presentation(kind, parts if kind != "text" else [])
