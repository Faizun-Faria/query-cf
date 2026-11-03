"""Phase 6: what the chat app needs that isn't drawing the page.

app/streamlit_app.py lays out the page; its settings, data, usage limits and analyst come from
here, so they can be tested without a browser.

The app reads the full warehouse when one is present, as on a laptop. Otherwise, as on
Streamlit Community Cloud, it downloads the demo's copy of the four mart tables once and checks
its SHA-256 before opening it (D-052). Three limits keep a public demo cheap (D-053): questions
per visit, a daily budget counted from the question log, and the demo API key's own monthly
spend limit in the Anthropic Console.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import requests
import yaml

from query_cf import analyst, guard, questionlog, semantic
from query_cf.tools import Toolbox

ROOT = Path(__file__).resolve().parent.parent
APP_FILE = ROOT / "config" / "app.yaml"
ENV_FILE = ROOT / ".env"  # tests point this elsewhere, so they never read real secrets
FULL_WAREHOUSE = ROOT / "data" / "warehouse" / "query_cf.duckdb"
DB_VARIABLE = "QUERY_CF_DB"
SOURCE = "app"  # tags every question the app asks, so its spending can be counted


class AppError(RuntimeError):
    """Something the app needs is missing or wrong; the message says what to do."""


@dataclass(frozen=True)
class Limits:
    questions_per_session: int
    max_question_chars: int
    daily_budget_usd: float


@dataclass(frozen=True)
class AppSettings:
    limits: Limits
    demo_url: str
    demo_sha256: str
    demo_file: Path
    memory_limit: str | None


def load_app_settings(path: Path = APP_FILE) -> AppSettings:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        limits, demo = data["limits"], data["demo_data"]
        settings = AppSettings(
            limits=Limits(
                int(limits["questions_per_session"]),
                int(limits["max_question_chars"]),
                float(limits["daily_budget_usd"]),
            ),
            demo_url=str(demo.get("url") or "").strip(),
            demo_sha256=str(demo.get("sha256") or "").strip().lower(),
            demo_file=Path(demo["file"]),
            memory_limit=str(data["duckdb_memory_limit"])
            if data.get("duckdb_memory_limit")
            else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(f"{path}: missing or invalid setting {exc}") from None
    if min(settings.limits.questions_per_session, settings.limits.max_question_chars) < 1:
        raise AppError(f"{path}: the limits must be at least 1")
    return settings


def load_secrets(secrets=None) -> None:
    """Make the API key and MongoDB address available as environment variables.

    Locally they come from .env; on Streamlit Community Cloud, from the app's secrets, whose
    top-level values Streamlit also sets as environment variables. Values already set win.
    """
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False)
    try:
        values = dict(secrets) if secrets is not None else {}
    except Exception:  # no secrets file: Streamlit raises rather than returning nothing
        values = {}
    for key, value in values.items():
        if isinstance(value, str):
            os.environ.setdefault(key, value)


# --- The data ----------------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, sha256: str, path: Path) -> None:
    """Fetch the demo data to a temporary name, and keep it only if its SHA-256 matches."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    digest = hashlib.sha256()
    try:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            with part.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    digest.update(chunk)
    except requests.RequestException as exc:
        part.unlink(missing_ok=True)
        raise AppError(f"Couldn't download the demo data: {str(exc).splitlines()[0]}") from None
    if digest.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise AppError("The demo data's SHA-256 doesn't match config/app.yaml, so it wasn't used.")
    part.replace(path)


def database_path(settings: AppSettings) -> Path:
    """The warehouse to read: QUERY_CF_DB, then the full warehouse, then the demo's copy."""
    configured = os.environ.get(DB_VARIABLE, "").strip()
    if configured:
        if not Path(configured).exists():
            raise AppError(f"{DB_VARIABLE} is {configured}, but there's no file there.")
        return Path(configured)
    if FULL_WAREHOUSE.exists():
        return FULL_WAREHOUSE
    demo = settings.demo_file if settings.demo_file.is_absolute() else ROOT / settings.demo_file
    if demo.exists() and settings.demo_sha256 and sha256_of(demo) == settings.demo_sha256:
        return demo
    if not (settings.demo_url and settings.demo_sha256):
        raise AppError(
            "There's no warehouse here, and config/app.yaml doesn't say where to download the "
            "demo data. Run the pipeline, or set demo_data url and sha256."
        )
    download(settings.demo_url, settings.demo_sha256, demo)
    return demo


def open_warehouse(path: Path, settings: AppSettings) -> duckdb.DuckDBPyConnection:
    """One locked, read-only connection; each visitor's analyst uses a cursor of it."""
    return guard.connect(path, memory_limit=settings.memory_limit)


# --- The analyst and its limits ----------------------------------------------------------------


def make_client():
    """The Claude API client; it reads ANTHROPIC_API_KEY. Tests replace this function."""
    import anthropic

    return anthropic.Anthropic()


def new_analyst(conn: duckdb.DuckDBPyConnection) -> analyst.Analyst:
    """An analyst for one visitor, with its own conversation and its own database cursor."""
    settings = analyst.load_settings()
    if not os.environ.get(analyst.API_KEY_VARIABLE, "").strip():
        raise AppError(
            f"{analyst.API_KEY_VARIABLE} isn't set. Add it to .env, or to the app's secrets."
        )
    log, _ = questionlog.open_log(settings.log_database, settings.log_collection, settings.log_file)
    toolbox = Toolbox(semantic.load_layer(), conn.cursor())
    return analyst.Analyst(make_client(), toolbox, settings, log)


def spent_today(log) -> float | None:
    """What the app's questions have cost since midnight UTC, or None if it can't be told."""
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return log.spent_since(midnight, SOURCE)
    except Exception:  # a database that can't be reached must stop spending, not guess
        return None


def refusal(question: str, asked: int, limits: Limits, spent: float | None) -> str | None:
    """Why the app won't send this question to the analyst, or None when it will."""
    if not question.strip():
        return "Type a question first."
    if len(question) > limits.max_question_chars:
        return f"Please keep a question under {limits.max_question_chars} characters."
    if asked >= limits.questions_per_session:
        return (
            f"This demo answers {limits.questions_per_session} questions per visit. "
            "Open the app again later to ask more."
        )
    if spent is None:
        return "The demo can't check today's spending right now, so it's paused. Try again later."
    if spent >= limits.daily_budget_usd:
        return "The demo has used today's budget. It resets at midnight UTC."
    return None
