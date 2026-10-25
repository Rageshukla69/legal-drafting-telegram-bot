"""Serializable multi-document intake state, style selection, single-page recreation state, undo history, and deterministic missing checks."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any
import json
from pathlib import Path

STYLE_NAMES = {
    "template_mode": "🏛️ मानक न्यायिक प्रारूप (Standard Judicial)",
    "dad_style": "⚖️ वरिष्ठ अधिवक्ता शैली (Senior Counsel)",
    "hybrid_mode": "✨ Vakil AI Pro — हाइब्रिड संस्करण",
    "as_is_mode": "📄 हूबहू / हस्तलिखित → लीगल पेपर (As-It-Is / Handwritten)",
    "single_page_mode": "📃 केवल एक पन्ना सुधार / री-क्रिएट (Single Page Recreate)",
}


@dataclass
class CaseState:
    case_id: str
    document_type: str = "dava_plaint"
    style_mode: str = "template_mode"
    style_selected: bool = False
    type_selected: bool = False
    selected_template_name: str = ""
    pending_point_action: str = ""
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
    # Single-page recreation state (for recreating/correcting a specific page like Page 3 of an old case)
    single_page_mode: bool = False
    target_page_number: int = 0
    page_correction_instruction: str = ""
    # All generated documents belonging to this case. Each item stores an independent
    # structured draft, while `facts` remains shared case evidence across document types.
    documents: list[dict[str, Any]] = field(default_factory=list)
    active_document_id: str = ""
    document_counter: int = 0

    def record(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text})
        self.history = self.history[-120:]
        if role == "user" and str(text or "").strip():
            clean_txt = str(text).strip()
            existing_raw = self.facts.get("raw_source_pages", [])
            if not isinstance(existing_raw, list):
                existing_raw = [str(existing_raw)] if existing_raw else []
            if not existing_raw or existing_raw[-1] != clean_txt:
                existing_raw.append(clean_txt)
            self.facts["raw_source_pages"] = existing_raw[-30:]
            self.facts["raw_source_text"] = "\n\n".join(self.facts["raw_source_pages"])

    def user_messages(self, limit: int = 40) -> list[str]:
        return [str(x.get("text", "")) for x in self.history if x.get("role") == "user"][-limit:]

    def merge_facts(self, new_facts: dict[str, Any]) -> None:
        """Merge semantic evidence without dropping earlier supported statements."""
        list_fields = {
            "plaintiffs", "defendants", "parties", "facts", "reliefs",
            "interim_reliefs", "demands", "defence_points", "documents",
            "custom_add_points", "custom_remove_points", "raw_source_pages",
        }
        for key, value in (new_facts or {}).items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            if key in list_fields:
                incoming = value if isinstance(value, list) else [value]
                existing = self.facts.get(key, [])
                if not isinstance(existing, list):
                    existing = [existing]
                seen = {str(x).strip().casefold() for x in existing if str(x).strip()}
                for item in incoming:
                    if isinstance(item, dict):
                        item = item.get("text") or item.get("content") or str(item)
                    item = str(item).strip()
                    marker = item.casefold()
                    if item and marker not in seen:
                        existing.append(item)
                        seen.add(marker)
                self.facts[key] = existing
            else:
                self.facts[key] = value

        # Generic party fields are derived only from explicitly extracted party roles.
        party_values = []
        for key in ("plaintiffs", "defendants"):
            value = self.facts.get(key, [])
            party_values.extend(value if isinstance(value, list) else [value])
        if party_values:
            existing = self.facts.get("parties", [])
            existing = existing if isinstance(existing, list) else [existing]
            seen = {str(x).strip().casefold() for x in existing if str(x).strip()}
            for item in party_values:
                item = str(item).strip()
                if item and item.casefold() not in seen:
                    existing.append(item)
                    seen.add(item.casefold())
            self.facts["parties"] = existing

    def set_draft(self, draft: dict[str, Any]) -> None:
        self.draft = draft or {}
        if not self.active_document_id:
            self.document_counter += 1
            self.active_document_id = f"{self.document_type}-{self.document_counter}"
        self.draft_version += 1
        self.draft_versions.append({"version": self.draft_version, "draft": self.draft})
        self.draft_versions = self.draft_versions[-10:]
        self.status = "drafted"
        item = {
            "document_id": self.active_document_id,
            "document_type": self.document_type,
            "style_mode": self.style_mode,
            "selected_template_name": self.selected_template_name,
            "version": self.draft_version,
            "draft": self.draft,
        }
        for i, existing in enumerate(self.documents):
            if existing.get("document_id") == self.active_document_id:
                self.documents[i] = item
                break
        else:
            self.documents.append(item)
        self.documents = self.documents[-20:]
        self.edit_mode = False
        self.pending_edit = {}
        self.pending_point_action = ""

    def undo_draft(self) -> bool:
        """Revert `self.draft` to the immediately preceding version in `self.draft_versions`."""
        if len(self.draft_versions) < 2:
            return False
        self.draft_versions.pop()
        prev_entry = self.draft_versions[-1]
        self.draft = prev_entry.get("draft") or {}
        self.draft_version = int(prev_entry.get("version", max(1, self.draft_version - 1)))
        for i, existing in enumerate(self.documents):
            if existing.get("document_id") == self.active_document_id:
                self.documents[i] = {
                    **existing,
                    "version": self.draft_version,
                    "draft": self.draft,
                }
                break
        self.pending_edit = {}
        self.pending_point_action = ""
        return True

    def archive_active_document(self) -> None:
        """Persist the current generated document into the case's multi-document list."""
        if not self.draft:
            return
        item = {
            "document_id": self.active_document_id or f"{self.document_type}-1",
            "document_type": self.document_type,
            "style_mode": self.style_mode,
            "selected_template_name": self.selected_template_name,
            "version": self.draft_version,
            "draft": self.draft,
        }
        replaced = False
        for i, existing in enumerate(self.documents):
            if existing.get("document_id") == item["document_id"]:
                self.documents[i] = item
                replaced = True
                break
        if not replaced:
            self.documents.append(item)
        self.documents = self.documents[-20:]

    def begin_new_document(self, document_type: str) -> None:
        """Keep shared case facts and selected style mode, but start a fresh document of the requested type."""
        self.archive_active_document()
        self.document_type = document_type
        self.type_selected = True
        self.selected_template_name = ""
        self.draft = {}
        self.draft_versions = []
        self.draft_version = 0
        self.edit_mode = False
        self.pending_edit = {}
        self.pending_point_action = ""
        self.single_page_mode = False
        self.target_page_number = 0
        self.page_correction_instruction = ""
        self.document_counter += 1
        self.active_document_id = f"{document_type}-{self.document_counter}"
        self.status = "collecting"

    def document_summaries(self) -> list[dict[str, Any]]:
        return [{
            "document_id": x.get("document_id", ""),
            "document_type": x.get("document_type", ""),
            "style_mode": x.get("style_mode", self.style_mode),
            "selected_template_name": x.get("selected_template_name", ""),
            "version": x.get("version", 0),
        } for x in self.documents]

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, data):
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in (data or {}).items() if k in allowed}
        return cls(**clean)

    def save(self, path): Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    @classmethod
    def load(cls, path): return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


DISPLAY_NAMES = {
    "dava_plaint": "Dava / वाद पत्र",
    "written_statement": "Written Statement / जवाब दावा",
    "application": "Application / प्रार्थना पत्र",
    "evidence_pw_affidavit": "Evidence / साक्ष्य शपथपत्र",
    "affidavit": "Affidavit / शपथपत्र",
    "legal_notice": "Legal Notice / विधिक नोटिस",
    "other_civil": "Other Civil Draft / अन्य सिविल ड्राफ्ट",
}

REQUIRED_BY_TYPE = {
    "dava_plaint": [("court_name", "न्यायालय का नाम"), ("plaintiffs", "वादी का नाम व पता"), ("defendants", "प्रतिवादी का नाम व पता"), ("facts", "मुख्य तथ्य / विवाद"), ("reliefs", "मांगी गई राहत (अनुतोष)")],
    "written_statement": [("court_name", "न्यायालय का नाम"), ("plaintiffs", "वादी का नाम"), ("defendants", "प्रतिवादी का नाम व पता"), ("facts", "जवाब दावा / प्रतिरक्षा के मुख्य तथ्य")],
    "application": [("court_name", "न्यायालय का नाम"), ("parties", "पक्षकारों का विवरण"), ("facts", "प्रार्थना पत्र के मुख्य तथ्य"), ("reliefs", "मांगी गई अंतरिम राहत")],
    "evidence_pw_affidavit": [("court_name", "न्यायालय का नाम"), ("deponent", "शपथकर्ता / साक्षी का विवरण"), ("facts", "साक्ष्य के मुख्य बिंदु")],
    "affidavit": [("deponent", "शपथकर्ता / शपथग्रहीता का नाम व पता"), ("facts", "शपथपत्र के कथन / विवरण"), ("purpose", "शपथपत्र का उद्देश्य")],
    "legal_notice": [("sender", "नोटिस प्रेषक का नाम व पता"), ("recipient", "नोटिस प्राप्तकर्ता का नाम व पता"), ("facts", "विवाद / लेन-देन का विवरण"), ("demands", "विधिक मांग एवं समयावधि")],
    "other_civil": [("parties", "पक्षकारों का विवरण"), ("facts", "मुख्य तथ्य"), ("purpose", "दस्तावेज़ का उद्देश्य")],
}


def missing_fields(facts, document_type="dava_plaint", style_mode: str = "template_mode"):
    facts = facts or {}
    if style_mode in {"as_is_mode", "single_page_mode"} or facts.get("as_is_mode") or facts.get("single_page_mode"):
        if facts.get("raw_source_text") or facts.get("facts") or facts.get("single_page_source_text"):
            return []
        return ["facts"]
    return [key for key, _ in REQUIRED_BY_TYPE.get(document_type, REQUIRED_BY_TYPE["other_civil"]) if not facts.get(key)]


def next_questions(facts, document_type="dava_plaint", max_questions=5, style_mode: str = "template_mode"):
    facts = facts or {}
    if style_mode in {"as_is_mode", "single_page_mode"} or facts.get("as_is_mode") or facts.get("single_page_mode"):
        if facts.get("raw_source_text") or facts.get("facts") or facts.get("single_page_source_text"):
            return []
        return [("facts", "दस्तावेज़ / पन्ने की Photo/PDF, हस्तलिखित पन्ना, या Text")]
    labels = REQUIRED_BY_TYPE.get(document_type, REQUIRED_BY_TYPE["other_civil"])
    return [(key, label) for key, label in labels if not facts.get(key)][:max_questions]