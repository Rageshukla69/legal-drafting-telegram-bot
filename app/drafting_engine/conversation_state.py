"""Case conversation state and missing-information loop.

The state is intentionally serializable so it can later be stored in Redis,
Postgres, SQLite, or another bot backend.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import json

@dataclass
class CaseState:
    case_id: str
    document_type: str = "dava_plaint"
    facts: dict[str, Any] = field(default_factory=dict)
    asked_fields: list[str] = field(default_factory=list)
    status: str = "collecting"
    history: list[dict[str, str]] = field(default_factory=list)

    def record(self, role: str, text: str):
        self.history.append({"role": role, "text": text})

    def merge_facts(self, new_facts: dict[str, Any]):
        for key, value in new_facts.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            self.facts[key] = value

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

from pathlib import Path

# Minimum facts needed before asking an LLM to compose a Dava.
REQUIRED_FIELDS = [
    ("court_name", "न्यायालय का नाम"),
    ("plaintiffs", "वादी का पूरा नाम/नाम"),
    ("defendants", "प्रतिवादी का पूरा नाम/नाम"),
    ("facts", "वाद के मुख्य तथ्य"),
    ("reliefs", "मांगी जाने वाली राहत"),
]

IMPORTANT_FIELDS = [
    ("plaintiff_intro", "वादी का परिचय/पता"),
    ("cause_of_action", "वाद-कारण, यदि अलग से उपलब्ध हो"),
    ("jurisdiction_facts", "क्षेत्राधिकार से संबंधित स्पष्ट तथ्य"),
    ("valuation", "वाद का मूल्यांकन, यदि उपलब्ध हो"),
    ("court_fee", "न्यायालय शुल्क, यदि उपलब्ध हो"),
]

def missing_fields(facts):
    missing = [k for k,_ in REQUIRED_FIELDS if not facts.get(k)]
    return missing

def next_questions(facts, max_questions=4):
    questions = []
    for key, label in REQUIRED_FIELDS + IMPORTANT_FIELDS:
        if not facts.get(key):
            questions.append((key, label))
        if len(questions) >= max_questions:
            break
    return questions
