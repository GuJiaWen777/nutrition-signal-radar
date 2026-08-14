from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect_database(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    database_path = path.resolve()
    if readonly:
        connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    if connection.in_transaction:
        raise RuntimeError("Nested transactions are not supported.")
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
