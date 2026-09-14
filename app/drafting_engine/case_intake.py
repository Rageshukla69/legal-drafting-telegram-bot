"""Conservative Gemini intake with deterministic parsing for clearly labelled case messages."""
from __future__ import annotations
import json, os, re
from pathlib import Path
from typing import Any
from .gemini_client import GeminiClient

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "intake_schema.json"

SYSTEM = """You are a conservative legal-document intake extractor for an Indian advocate's drafting system.
Extract ONLY facts explicitly stated in the user's message and current case state.
Never infer names, dates, addresses, relationships, ownership, title, possession,
statutes, limitation, valuation, court fee, jurisdiction, documents, allegations,
causes of action, defences or reliefs that are not explicitly supported.
Preserve Hindi wording and material event status exactly. An attempt is not completion;
a threat is not dispossession; an allegation is not an established fact.
If the user explicitly corrects a prior fact, replace the contradicted value.
Keep facts separate from requested relief/demand/defence.
Return only JSON matching the supplied schema.
"""

# These labels are intentionally conservative: they only activate when the user
# clearly supplied a labelled block, preventing the fallback parser from inventing facts.
LABELS = {
    "court_name": [r"Court", r"न्यायालय"],
    "plaintiff_intro": [r"Plaintiff", r"वादी"],
    "defendant_intro": [r"Defendant", r"प्रतिवादी"],
    "property_description": [r"Property", r"संपत्ति", r"विवादित भूमि"],
    "facts": [r"Main Facts", r"Facts", r"मुख्य तथ्य", r"तथ्य"],
    "cause_of_action": [r"Cause of action", r"वाद हेतुक"],
    "reliefs": [r"Relief", r"Reliefs", r"राहत", r"प्रार्थना"],
    "purpose": [r"Purpose", r"उद्देश्य"],
    "sender": [r"Sender", r"प्रेषक"],
    "recipient": [r"Recipient", r"प्राप्तकर्ता"],
    "deponent": [r"Deponent", r"शपथकर्ता"],
}


def _extract_labelled(message: str) -> dict[str, Any]:
    """Extract obvious `Label: value` blocks without interpreting their content."""
    text = message.replace("\r\n", "\n").replace("\r", "\n")
    # Build one alternation and capture until the next known label or end.
    all_labels = []
    canonical_for = {}
    for key, variants in LABELS.items():
        for v in variants:
            all_labels.append(v)
            canonical_for[v.lower()] = key
    if not all_labels:
        return {}
    label_alt = "|".join(sorted((re.escape(x) for x in all_labels), key=len, reverse=True))
    pattern = re.compile(rf"(?im)(?<!\w)(?P<label>{label_alt})\s*:\s*(?P<value>.*?)(?=\n\s*(?:{label_alt})\s*:|\Z)", re.S)
    out: dict[str, Any] = {}
    for m in pattern.finditer(text):
        raw_label = m.group("label").strip().lower()
        key = canonical_for.get(raw_label)
        value = m.group("value").strip()
        if not key or not value:
            continue
        if key in {"facts", "reliefs"}:
            # Preserve each line as a separate fact/relief when possible.
            items = [re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", x).strip() for x in value.splitlines()]
            items = [x for x in items if x]
            out[key] = items or [value]
        else:
            out[key] = value
    # Derive party arrays from the exact labelled intros; no new factual content.
    if out.get("plaintiff_intro"):
        out["plaintiffs"] = [out["plaintiff_intro"]]
    if out.get("defendant_intro"):
        out["defendants"] = [out["defendant_intro"]]
    return out


def extract_case_facts(message: str, current_facts: dict[str, Any] | None = None, document_type: str = "dava_plaint") -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = {"document_type": document_type, "current_case_facts": current_facts or {}, "new_user_message": message}
    model_result: dict[str, Any] = {}
    try:
        model_result = GeminiClient().generate_json(
            system=SYSTEM,
            prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            schema=schema,
            thinking_level=os.getenv("GEMINI_INTAKE_THINKING_LEVEL", "medium"),
        ) or {}
    except Exception:
        model_result = {}
    # Explicit labels are stronger evidence than an imperfect model extraction.
    labelled = _extract_labelled(message)
    merged = dict(model_result)
    for key, value in labelled.items():
        merged[key] = value
    return merged
