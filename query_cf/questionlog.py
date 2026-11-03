"""Phase 4: the question log (US-11).

Every question gets one record: the question, each tool call with its input, SQL, rows and
warnings, the answer, the model that served it, tokens, cost and timestamps. Records go to
MongoDB Atlas when QUERY_CF_MONGODB_URI is set, and to a local JSON Lines file otherwise. If
MongoDB can't be reached, the record goes to the file instead, so no answer is ever unlogged
(D-037).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

URI_VARIABLE = "QUERY_CF_MONGODB_URI"
URI_SCHEMES = ("mongodb+srv://", "mongodb://")
DEFAULT_FILE = Path("data") / "logs" / "questions.jsonl"


class FileLog:
    def __init__(self, path: str | Path = DEFAULT_FILE):
        self.path = Path(path)

    def write(self, record: dict) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return f"{self.path} (question {record['question_id']})"


class MongoLog:
    """Writes to <database>.<collection>; falls back to a FileLog when the write fails."""

    def __init__(self, collection, fallback: FileLog, name: str):
        self.collection = collection
        self.fallback = fallback
        self.name = name

    @classmethod
    def connect(cls, uri: str, database: str, collection: str, fallback: FileLog) -> MongoLog:
        from pymongo import MongoClient  # only needed when MongoDB is configured

        client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="query-cf")
        return cls(client[database][collection], fallback, f"{database}.{collection}")

    def write(self, record: dict) -> str:
        from pymongo.errors import PyMongoError

        try:
            self.collection.insert_one(dict(record))  # insert_one adds _id to the dict it gets
        except PyMongoError as exc:
            where = self.fallback.write(record)
            return f"{where}; MongoDB failed: {str(exc).splitlines()[0][:120]}"
        return f"MongoDB {self.name} (question {record['question_id']})"


def check_mongo(uri: str, database: str) -> None:
    """Write and delete one document, which proves the address, password and permissions."""
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="query-cf")
    try:
        checks = client[database]["connection_checks"]
        inserted = checks.insert_one({"checked_at": datetime.now(UTC)})
        checks.delete_one({"_id": inserted.inserted_id})
    finally:
        client.close()


def describe_mongo_error(exc: Exception) -> str:
    from pymongo import errors

    first_line = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
    if isinstance(exc, errors.ConfigurationError | errors.InvalidURI):
        return f"the connection string in {URI_VARIABLE} isn't valid ({first_line})"
    if isinstance(exc, errors.OperationFailure) and exc.code in (8000, 18):
        return "the username or password was rejected; reset the database user's password"
    if isinstance(exc, errors.OperationFailure):
        return f"the database user can't write to the database ({first_line})"
    if isinstance(exc, errors.ServerSelectionTimeoutError):
        return "couldn't reach the cluster; add your current IP address to the Atlas IP access list"
    return first_line


def open_log(
    database: str, collection: str, file: str | Path = DEFAULT_FILE
) -> tuple[FileLog | MongoLog, str | None]:
    """The log to write to, and a problem to report if MongoDB is set up but unusable."""
    from_file = FileLog(file)
    uri = os.environ.get(URI_VARIABLE, "").strip()
    if not uri:
        return from_file, None
    if not uri.startswith(URI_SCHEMES):
        return from_file, f"{URI_VARIABLE} doesn't start with {' or '.join(URI_SCHEMES)}"
    try:
        return MongoLog.connect(uri, database, collection, from_file), None
    except Exception as exc:  # a malformed URI or a missing driver: keep logging to the file
        return from_file, f"{URI_VARIABLE} can't be used ({str(exc).splitlines()[0][:120]})"
