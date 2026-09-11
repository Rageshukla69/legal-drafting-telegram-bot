from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.drafting_engine.conversation_state import CaseState

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cases.sqlite3"


class CaseStore:
    """Small SQLite-backed store for the current Telegram user's case."""

    def __init__(self, path: str | Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    state_json TEXT NOT NULL
                )"""
            )
            db.commit()

    def get(self, case_id: str) -> CaseState | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT state_json FROM cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return CaseState.from_dict(json.loads(row[0])) if row else None

    def save(self, user_id: int | str, state: CaseState) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """INSERT OR REPLACE INTO cases(case_id, user_id, state_json)
                   VALUES (?, ?, ?)""",
                (
                    state.case_id,
                    str(user_id),
                    json.dumps(state.to_dict(), ensure_ascii=False),
                ),
            )
            db.commit()

    def delete(self, case_id: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))
            db.commit()
