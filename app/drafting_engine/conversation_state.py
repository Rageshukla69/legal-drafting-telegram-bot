"""Serializable Dava intake state and deterministic missing-information checks."""
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
        self.history = self.history[-40:]

    def merge_facts(self, new_facts: dict[str, Any]) -> None:
        list_fields = {
            "plaintiffs", "defendants", "facts", "reliefs", "interim_reliefs", "documents"
        }
        for key, value in new_facts.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            if key in list_fields:
                incoming = value if isinstance(value, list) else [value]
                existing = self.facts.get(key, [])
                if not isinstance(existing, list):
                    existing = [existing]
                for item in incoming:
                    item = str(item).strip()
                    if item and item not in existing:
                        existing.append(item)
                self.facts[key] = existing
            else:
                self.facts[key] = value

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


REQUIRED_FIELDS = [
    ("court_name", "न्यायालय का नाम"),
    ("plaintiffs", "वादी का/वादियों का पूरा नाम"),
    ("defendants", "प्रतिवादी का/प्रतिवादियों का पूरा नाम"),
    ("facts", "वाद के मुख्य तथ्य"),
    ("reliefs", "मांगी जाने वाली राहत"),
]

IMPORTANT_FIELDS = [
    ("plaintiff_intro", "वादी का पूरा परिचय/पता"),
    ("defendant_intro", "प्रतिवादी का पूरा परिचय/पता"),
    ("property_description", "विवादित संपत्ति का पूरा विवरण"),
    ("cause_of_action", "वाद-कारण और वह कब उत्पन्न हुआ"),
    ("jurisdiction_facts", "इस न्यायालय के क्षेत्राधिकार के तथ्य"),
    ("limitation_facts", "समय-सीमा से संबंधित तथ्य, यदि उपलब्ध हों"),
    ("valuation", "वाद का मूल्यांकन, यदि उपलब्ध हो"),
    ("court_fee", "न्यायालय शुल्क की उपलब्ध जानकारी"),
]


def missing_fields(facts):
    return [key for key, _ in REQUIRED_FIELDS if not facts.get(key)]


def next_questions(facts, max_questions=5):
    result = []
    for key, label in REQUIRED_FIELDS + IMPORTANT_FIELDS:
        if not facts.get(key):
            result.append((key, label))
        if len(result) >= max_questions:
            break
    return result
