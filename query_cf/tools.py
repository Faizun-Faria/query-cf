"""Phase 4: the three tools the AI analyst can call (D-030).

    query_metrics   a request to the semantic layer: the first choice for every question
    list_values     the exact published values of one dimension, such as company names
    run_sql         one guarded read-only query, when no metric request can express the question

Each tool takes the JSON input the model sent and returns a JSON-ready dict. Values come
back already formatted the way answers write them (1,234 and 12.3%), so the model copies
numbers rather than reformatting them. A ToolError is a problem with the request, which the
model is told about so it can try again.
"""

from __future__ import annotations

from datetime import date

import duckdb

from query_cf import guard
from query_cf.semantic import (
    TIME_GRAINS,
    MetricQuery,
    SemanticError,
    SemanticLayer,
    compile_query,
    format_value,
)

MAX_SQL_ROWS = 200
MAX_VALUES = 25
TIMEOUT_SECONDS = 30.0
PERCENT_SUFFIXES = ("_rate", "_share")


class ToolError(ValueError):
    """The tool input can't be run as given; the message says what to change."""


def _strings(payload: dict, key: str) -> list[str]:
    value = payload.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ToolError(f"{key} must be a list of names")
    return [v.strip() for v in value]


def _date(payload: dict, key: str) -> date | None:
    value = payload.get(key)
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolError(f"{key} must be a date written YYYY-MM-DD, not {value!r}") from None


def _check_keys(payload: dict, allowed: set[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ToolError(
            f"unknown input {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}"
        )


class Toolbox:
    """The analyst's tools over one read-only warehouse connection."""

    def __init__(
        self,
        layer: SemanticLayer,
        conn: duckdb.DuckDBPyConnection,
        *,
        max_rows: int = MAX_SQL_ROWS,
        timeout: float = TIMEOUT_SECONDS,
    ):
        self.layer = layer
        self.conn = conn
        self.max_rows = max_rows
        self.timeout = timeout

    def run(self, name: str, payload: dict) -> dict:
        tools = {
            "query_metrics": self.query_metrics,
            "list_values": self.list_values,
            "run_sql": self.run_sql,
        }
        if name not in tools:
            raise ToolError(f"there is no tool called {name}")
        if not isinstance(payload, dict):
            raise ToolError("the tool input must be a JSON object")
        return tools[name](payload)

    def _execute(self, sql: str, params: list | None = None, max_rows: int | None = None):
        try:
            return guard.execute(self.conn, sql, params, timeout=self.timeout, max_rows=max_rows)
        except guard.QueryTimeout as exc:
            raise ToolError(str(exc)) from None
        except duckdb.Error as exc:
            raise ToolError(
                f"the warehouse rejected the query: {str(exc).splitlines()[0]}"
            ) from None

    # --- query_metrics --------------------------------------------------------------------

    def query_metrics(self, payload: dict) -> dict:
        _check_keys(
            payload,
            {
                "metrics",
                "group_by",
                "filters",
                "named_filters",
                "received_from",
                "received_to",
                "top",
            },
        )
        filters: dict[str, list[str]] = {}
        for item in payload.get("filters") or []:
            if not isinstance(item, dict) or set(item) != {"dimension", "values"}:
                raise ToolError("each filter must be {dimension, values}")
            values = item["values"] if isinstance(item["values"], list) else [item["values"]]
            filters.setdefault(str(item["dimension"]), []).extend(str(v) for v in values)
        top = payload.get("top")
        if top is not None and (not isinstance(top, int) or isinstance(top, bool) or top < 1):
            raise ToolError("top must be a whole number of 1 or more")

        request = MetricQuery(
            metrics=tuple(_strings(payload, "metrics")),
            by=tuple(_strings(payload, "group_by")),
            filters={name: tuple(values) for name, values in filters.items()},
            segments=tuple(_strings(payload, "named_filters")),
            received_from=_date(payload, "received_from"),
            received_to=_date(payload, "received_to"),
            # One row more than asked shows whether the next row ties with the last one (D-034).
            top=top + 1 if top else None,
        )
        try:
            compiled = compile_query(self.layer, request)
            matched = compile_query(
                self.layer,
                MetricQuery(
                    metrics=("complaints",),
                    filters=request.filters,
                    segments=request.segments,
                    received_from=request.received_from,
                    received_to=request.received_to,
                ),
            )
        except SemanticError as exc:
            raise ToolError(str(exc)) from None

        columns, rows = self._execute(compiled.sql)
        result = {}
        if top and len(rows) > top:
            first_metric = len(request.by)
            if rows[top][first_metric] == rows[top - 1][first_metric]:
                tied = format_value(
                    rows[top][first_metric],
                    columns[first_metric] in compiled.percent_columns,
                    columns[first_metric],
                )
                result["tie_at_cutoff"] = (
                    f"The next row also has {tied}. Ask again with a larger top to list every "
                    "row tied at the cut-off."
                )
            rows = rows[:top]
        percent = set(compiled.percent_columns)
        result = {
            "columns": columns,
            "rows": [
                [format_value(v, c in percent, c) for c, v in zip(columns, row, strict=True)]
                for row in rows
            ],
            "rows_returned": len(rows),
            "complaints_matched": format_value(self._execute(matched.sql)[1][0][0]),
            **result,
            "notes": list(compiled.notes),
            "warnings": list(compiled.caveats),
            "sql": compiled.sql,
        }
        return result

    # --- list_values ------------------------------------------------------------------------

    def list_values(self, payload: dict) -> dict:
        _check_keys(payload, {"dimension", "contains"})
        name = payload.get("dimension")
        dim = self.layer.dimensions.get(str(name))
        if dim is None or dim.type != "category":
            names = ", ".join(
                d.name for d in self.layer.dimensions.values() if d.type == "category"
            )
            raise ToolError(f"list_values works on these dimensions: {names}")
        contains = str(payload.get("contains") or "").strip()

        sql = [
            f"select {dim.sql} as value, count(*) as complaints",
            f"from {self.layer.base_table} as {self.layer.base_alias}",
        ]
        if dim.join:
            join = self.layer.joins[dim.join]
            sql.append(f"join {join.table} as {join.alias} on {join.on}")
        sql.append(f"where {dim.sql} is not null")
        if contains:
            sql.append(f"  and strpos(lower({dim.sql}), lower(?)) > 0")
        sql += ["group by 1", "order by 2 desc, 1", f"limit {MAX_VALUES + 1}"]
        sql_text = "\n".join(sql)
        _, rows = self._execute(sql_text, [contains] if contains else None)

        values = []
        for value, complaints in rows[:MAX_VALUES]:
            entry = {"value": value, "complaints": format_value(complaints)}
            if dim.name == "company" and self.layer.companies.get(value):
                entry["short_names"] = list(self.layer.companies[value])
            values.append(entry)
        return {
            "dimension": dim.name,
            "contains": contains,
            "values": values,
            "more_values": len(rows) > MAX_VALUES,
            "note": "Use a value exactly as written here in a filter or in SQL.",
            "sql": sql_text,
        }

    # --- run_sql ----------------------------------------------------------------------------

    def run_sql(self, payload: dict) -> dict:
        _check_keys(payload, {"sql", "reason", "follows"})
        sql = str(payload.get("sql") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if not sql:
            raise ToolError("sql is empty")
        if not reason:
            raise ToolError("give the reason no query_metrics call can answer the question")
        follows = _strings(payload, "follows")
        unknown = [m for m in follows if m not in self.layer.metrics]
        if unknown:
            raise ToolError(f"unknown metric in follows: {', '.join(unknown)}")

        guard.check_sql(sql)  # raises BlockedQuery, which the analyst logs as a block
        columns, rows = self._execute(sql, max_rows=self.max_rows)
        truncated = len(rows) > self.max_rows
        rows = rows[: self.max_rows]

        caveat_ids = (
            ["reports_not_findings"] if "reports_not_findings" in self.layer.caveats else []
        )
        for metric in follows:
            caveat_ids += [c for c in self.layer.metrics[metric].caveats if c not in caveat_ids]
        return {
            "columns": columns,
            "rows": [
                [
                    format_value(v, c.endswith(PERCENT_SUFFIXES), c)
                    for c, v in zip(columns, row, strict=True)
                ]
                for row in rows
            ],
            "rows_returned": len(rows),
            "truncated": truncated,
            "warnings": [self.layer.caveats[c] for c in caveat_ids],
            "sql": sql,
        }

    # --- Definitions sent to the model ---------------------------------------------------------

    def definitions(self) -> list[dict]:
        """The tool definitions, built from the semantic layer so names can only be valid ones."""
        layer = self.layer
        metrics = list(layer.metrics)
        time = layer.time_dimension
        group_by = [f"{time.name}:{g}" for g in TIME_GRAINS] + [
            d.name for d in layer.dimensions.values() if d.type != "time"
        ]
        filterable = [d.name for d in layer.dimensions.values() if d.type != "time"]
        categories = [d.name for d in layer.dimensions.values() if d.type == "category"]
        names = {"type": "array", "items": {"type": "string", "enum": metrics}}
        return [
            {
                "name": "query_metrics",
                "description": (
                    "Answer from the semantic layer's defined metrics. Call this first for every "
                    "question a defined metric can answer; a question with several steps gets "
                    "several calls. Returns the result rows already formatted (counts with "
                    "commas, rates as percentages), how many complaints matched the filters, "
                    "notes on how names were read, and the warnings the answer must show. "
                    "With top, a tie at the cut-off is reported."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "metrics": {
                            **names,
                            "description": "One or more metrics; top ranks by the first.",
                        },
                        "group_by": {
                            "type": "array",
                            "items": {"type": "string", "enum": group_by},
                            "description": (
                                f"Dimensions to group by; {time.name}:<grain> groups by the "
                                "date received."
                            ),
                        },
                        "filters": {
                            "type": "array",
                            "description": (
                                "Keep only complaints matching every filter; several values "
                                "mean any of them."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "dimension": {"type": "string", "enum": filterable},
                                    "values": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["dimension", "values"],
                            },
                        },
                        "named_filters": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(layer.segments)},
                        },
                        "received_from": {
                            "type": "string",
                            "description": "First date received, YYYY-MM-DD.",
                        },
                        "received_to": {
                            "type": "string",
                            "description": "Last date received, YYYY-MM-DD.",
                        },
                        "top": {
                            "type": "integer",
                            "description": "Keep the N rows with the highest first metric.",
                        },
                    },
                    "required": ["metrics"],
                },
            },
            {
                "name": "list_values",
                "description": (
                    "Look up the exact published values of one dimension that contain some text, "
                    "with their complaint counts, such as company names containing 'citizens'. "
                    "Call this when a question names a company that isn't in the company list, or "
                    "when you need a value's exact spelling. Matching part of a name is only for "
                    "finding names: filter on the exact value it returns."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "dimension": {"type": "string", "enum": categories},
                        "contains": {
                            "type": "string",
                            "description": "Text the value contains, any case.",
                        },
                    },
                    "required": ["dimension"],
                },
            },
            {
                "name": "run_sql",
                "description": (
                    "Run one read-only DuckDB query on "
                    f"{guard.allowed_tables()}. Use it only when no query_metrics call can express "
                    "the question: a minimum number of complaints per group, the lowest values "
                    "first, a count of companies or groups that pass a test, or a ranking inside "
                    "each group. Copy each metric's definition from the catalog exactly. Name rate "
                    "and share columns ending in _rate or _share and compute them as percentages "
                    "from 0 to 100. Anything else is blocked. At most "
                    f"{self.max_rows} rows come back."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string"},
                        "reason": {
                            "type": "string",
                            "description": "Why no query_metrics call can answer it.",
                        },
                        "follows": {
                            **names,
                            "description": "The metrics whose definitions the SQL copies.",
                        },
                    },
                    "required": ["sql", "reason", "follows"],
                },
            },
        ]
