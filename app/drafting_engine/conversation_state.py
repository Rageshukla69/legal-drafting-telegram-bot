"""Serializable case conversation state and conservative missing-information checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
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

    def record(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text})

    def merge_facts(self, new_facts: dict[str, Any]) -> None:
        for key, value in new_facts.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            self.facts[key] = value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseState":
        return cls(**data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "CaseState":
        return cls.from_dict(
            json.loads(Path(path).read_text(encoding="utf-8"))
        )


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


def missing_fields(facts: dict[str, Any]) -> list[str]:
    return [key for key, _ in REQUIRED_FIELDS if not facts.get(key)]


def next_questions(
    facts: dict[str, Any], max_questions: int = 4
) -> list[tuple[str, str]]:
    questions: list[tuple[str, str]] = []
    for key, label in REQUIRED_FIELDS + IMPORTANT_FIELDS:
        if not facts.get(key):
            questions.append((key, label))
        if len(questions) >= max_questions:
            break
    return questions
