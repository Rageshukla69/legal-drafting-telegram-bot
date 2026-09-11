"""AI structural planner for advocate-grade Dava/Plaint documents."""
from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any
from .azure_client import _azure_client

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "structure_schema.json"

SYSTEM = """You are the structural planning layer of a conservative Indian civil-pleading drafting system.
Your job is NOT to draft the plaint. Decide the case-specific pleading architecture before a separate drafting model writes it.
Use only supplied case facts and advocate-corpus reference. Never invent facts.

NON-NEGOTIABLE:
- Plan a genuine civil Dava/Plaint, not a summary or report.
- Party identity belongs in the party block, NOT in numbered pleadings.
- Never plan generic metadata headings such as वादी का परिचय, प्रतिवादी का परिचय, विवादित संपत्ति, or वाद के तथ्य.
- Every numbered paragraph must have one distinct substantive pleading purpose.
- Preserve chronology and factual modality: attempt is not completion; threat is not dispossession; possession is not ownership/title.
- Cause of action and jurisdiction must be distinct and supported by supplied facts.
- Do not plan limitation, valuation or court-fee averments unless their facts are supplied.
- Do not plan unsupported remedies.
- Renderer assigns numbering; do not include numbers in paragraph purposes.
- Verification may be included when a verifying party is identifiable.
Return only the supplied JSON schema."""

PARTY_SECTIONS = {"intro", "party_identity", "metadata"}
PARTY_PURPOSES = {"वादी परिचय", "प्रतिवादी परिचय", "वादी का परिचय", "प्रतिवादी का परिचय", "plaintiff introduction", "defendant introduction", "party introduction"}


def plan_structure(facts: dict[str, Any], retrieval_context: str) -> dict[str, Any]:
    client = _azure_client()
    payload = {"case_facts": facts, "advocate_corpus_reference": retrieval_context, "instruction": "Create only the case-specific pleading architecture. Do not draft final paragraphs. Do not create party-introduction paragraphs."}
    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
        response_format={"type": "json_schema", "json_schema": {"name": "dava_structure", "strict": True, "schema": json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))}},
        temperature=0,
    )
    return normalize_structure(json.loads(response.choices[0].message.content), facts)


def _standard_title(plan_title: str, nature: str, reliefs: Any) -> str:
    title = plan_title.strip()
    low = title.casefold()
    relief_text = " ".join(str(x) for x in (reliefs or [])).casefold()
    if "निषेधाज्ञा" in title or "injunction" in low or "injunction" in nature.casefold() or "निषेधाज्ञा" in relief_text:
        return "वादपत्र वास्ते स्थायी निषेधाज्ञा"
    return title if title not in {"", "वादपत्र", "दावा", "दावापत्र"} else "वादपत्र"


def normalize_structure(plan: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    paragraphs, seen = [], set()
    for raw in plan.get("paragraphs", []):
        item = dict(raw)
        purpose = str(item.get("purpose", "")).strip()
        section = str(item.get("section", "other")).strip()
        if section in PARTY_SECTIONS or purpose.casefold() in {x.casefold() for x in PARTY_PURPOSES}:
            continue
        if not purpose:
            continue
        key = (section, purpose.casefold())
        if key in seen:
            continue
        seen.add(key)
        item["section"] = section
        item["purpose"] = purpose
        item["source_fields"] = [str(x) for x in item.get("source_fields", []) if str(x).strip()]
        paragraphs.append(item)

    def present(k: str) -> bool:
        return bool(facts.get(k) not in (None, "", [], {}))

    filtered = []
    for item in paragraphs:
        section = item["section"]
        if section == "jurisdiction" and not present("jurisdiction_facts"):
            continue
        if section == "limitation" and not present("limitation_facts"):
            continue
        if section == "valuation_court_fee" and not (present("valuation") or present("court_fee")):
            continue
        filtered.append(item)
    filtered.sort(key=lambda x: int(x.get("order", 9999)))
    for idx, item in enumerate(filtered, 1):
        item["order"] = idx

    opening = str(plan.get("opening", "")).strip()
    if not opening or opening in {"कुछ भी", "वादी निम्नलिखित निवेदन करता है:-"}:
        opening = "वादी निम्नानुसार निवेदन करता है:-"

    return {
        "document_type": "dava_plaint",
        "nature": str(plan.get("nature", "civil plaint")).strip(),
        "title": _standard_title(str(plan.get("title", "")), str(plan.get("nature", "")), facts.get("reliefs")),
        "opening": opening,
        "paragraphs": filtered,
        "prayer_items": [str(x).strip() for x in plan.get("prayer_items", []) if str(x).strip()],
        "include_verification": bool(plan.get("include_verification", False)),
        "warnings": [str(x).strip() for x in plan.get("warnings", []) if str(x).strip()],
    }
