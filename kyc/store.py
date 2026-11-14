"""SQLite (stdlib) as the integrity engine: DDL enforces PK / FK / CHECK / UNIQUE on load."""
from __future__ import annotations

import sqlite3
from datetime import date

from .schema import column_names, ddl


def _py(v):
    return v.isoformat() if isinstance(v, date) else v


def to_db(T: dict, tables: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(ddl(tables))
    conn.execute("BEGIN")
    conn.execute("PRAGMA defer_foreign_keys=ON")  # checked at COMMIT: parents may be inserted in any order
    for t in tables:
        cols = column_names(t)
        rows = [tuple(_py(r.get(c)) for c in cols) for r in sorted(T[t["name"]], key=lambda x: tuple(x[k] for k in t["order"]))]
        conn.executemany(f'INSERT INTO {t["name"]} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})', rows)
    conn.commit()
    return conn


def date_columns(table: dict) -> list[str]:
    return [c["name"] for c in table["cols"] if c["unit"] == "date"]


def load_tables(conn: sqlite3.Connection, tables: list[dict]) -> dict:
    """Read rows back as dicts with real `date` objects (so rules.py runs on persisted data)."""
    out = {}
    for t in tables:
        cols = column_names(t)
        dcols = set(date_columns(t))
        rows = []
        for rec in conn.execute(f'SELECT {",".join(cols)} FROM {t["name"]} ORDER BY {",".join(t["order"])}'):
            row = dict(zip(cols, rec))
            for c in dcols:
                if row[c] is not None:
                    row[c] = date.fromisoformat(row[c])
            rows.append(row)
        out[t["name"]] = rows
    return out
