"""Serializable multi-document intake state and deterministic missing checks."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any
import json
from pathlib import Path

@dataclass
class CaseState:
    case_id: str
    document_type: str = "dava_plaint"
    facts: dict[str, Any] = field(default_factory=dict)
    asked_fields: list[str] = field(default_factory=list)
    status: str = "collecting"
    history: list[dict[str, str]] = field(default_factory=list)
    draft_version: int = 0
    last_error: str = ""
    # Post-draft editor state. Defaults keep old Cosmos/SQLite records compatible.
    draft: dict[str, Any] = field(default_factory=dict)
    draft_versions: list[dict[str, Any]] = field(default_factory=list)
    edit_mode: bool = False
    pending_edit: dict[str, Any] = field(default_factory=dict)

    def record(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text})
        self.history = self.history[-80:]

    def user_messages(self, limit: int = 24) -> list[str]:
        return [str(x.get("text", "")) for x in self.history if x.get("role") == "user"][-limit:]

    def merge_facts(self, new_facts: dict[str, Any]) -> None:
        list_fields = {"plaintiffs","defendants","parties","facts","reliefs","interim_reliefs","demands","defence_points","documents"}
        for key, value in (new_facts or {}).items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            if key in list_fields:
                incoming = value if isinstance(value, list) else [value]
                existing = self.facts.get(key, [])
                if not isinstance(existing, list): existing = [existing]
                for item in incoming:
                    if isinstance(item, dict):
                        item = item.get("text") or item.get("content") or str(item)
                    item = str(item).strip()
                    if item and item not in existing:
                        existing.append(item)
                self.facts[key] = existing
            else:
                self.facts[key] = value

    def set_draft(self, draft: dict[str, Any]) -> None:
        self.draft = draft or {}
        self.draft_version += 1
        self.draft_versions.append({"version": self.draft_version, "draft": self.draft})
        self.draft_versions = self.draft_versions[-10:]
        self.status = "drafted"
        self.edit_mode = False
        self.pending_edit = {}

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, data):
        # Ignore unknown legacy keys rather than making an old saved case unloadable.
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**clean)

    def save(self, path): Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    @classmethod
    def load(cls, path): return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

DISPLAY_NAMES = {
    "dava_plaint":"Dava / वाद पत्र",
    "written_statement":"Written Statement / लिखित कथन",
    "application":"Application / प्रार्थना पत्र",
    "evidence_pw_affidavit":"Evidence / PW Affidavit",
    "affidavit":"Affidavit / शपथपत्र",
    "legal_notice":"Legal Notice / विधिक नोटिस",
    "other_civil":"Other Civil Draft / अन्य सिविल ड्राफ्ट",
}

REQUIRED_BY_TYPE = {
    "dava_plaint": [("court_name","न्यायालय"),("plaintiffs","वादी"),("defendants","प्रतिवादी"),("facts","मुख्य तथ्य"),("reliefs","राहत")],
    "written_statement": [("court_name","न्यायालय"),("plaintiffs","वादी"),("defendants","प्रतिवादी"),("facts","जवाब/प्रतिरक्षा के तथ्य")],
    "application": [("court_name","न्यायालय"),("parties","पक्षकार"),("facts","मुख्य तथ्य"),("reliefs","मांगी गई राहत")],
    "evidence_pw_affidavit": [("court_name","न्यायालय"),("deponent","शपथकर्ता/साक्षी"),("facts","साक्ष्य के तथ्य")],
    "affidavit": [("deponent","शपथकर्ता"),("facts","शपथपत्र के तथ्य"),("purpose","शपथपत्र का उद्देश्य")],
    "legal_notice": [("sender","प्रेषक"),("recipient","प्राप्तकर्ता"),("facts","नोटिस के तथ्य"),("demands","मांग/अनुपालन")],
    "other_civil": [("parties","पक्षकार"),("facts","मुख्य तथ्य"),("purpose","ड्राफ्ट का उद्देश्य")],
}

def missing_fields(facts, document_type="dava_plaint"):
    return [key for key, _ in REQUIRED_BY_TYPE.get(document_type, REQUIRED_BY_TYPE["other_civil"]) if not facts.get(key)]

def next_questions(facts, document_type="dava_plaint", max_questions=5):
    labels = REQUIRED_BY_TYPE.get(document_type, REQUIRED_BY_TYPE["other_civil"])
    return [(key,label) for key,label in labels if not facts.get(key)][:max_questions]
