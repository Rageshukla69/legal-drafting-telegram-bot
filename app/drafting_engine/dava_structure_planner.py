"""AI structural planner for Dava/Plaint documents.

This module deliberately plans the pleading before the drafting model writes it.
It does not draft facts and it must not fill missing case information.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

from .azure_client import _azure_client

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "structure_schema.json"

SYSTEM = """You are the structural planning layer of a conservative Indian civil-pleading drafting system.
Your job is NOT to draft the plaint. Your job is to decide the most appropriate structure for THIS case before a separate drafting model writes it.

Use the supplied case facts and the supplied advocate-corpus reference only.
The corpus is a style/organization reference, not a source of facts.
Never invent names, dates, addresses, property details, rights, events, statutes, limitation facts, valuation, court fee, jurisdiction facts or reliefs.

IMPORTANT:
- Plan a genuine civil Dava/Plaint, not a case-summary report.
- Prefer a coherent numbered sequence of pleading averments.
- Do not create generic metadata headings such as 'वादी का परिचय', 'प्रतिवादी का परिचय', 'विवादित संपत्ति' or 'वाद के तथ्य'.
- Property facts should be placed where they naturally support the pleaded right/possession/dispute.
- Chronology should be preserved.
- Cause of action, jurisdiction, limitation, and valuation/court-fee should be included only when supported by supplied facts.
- Do not create a paragraph merely because it is conventional if the necessary facts are absent.
- A requested relief is not a past event and must never be planned as one.
- 'Attempted', 'threatened', and 'apprehended' must remain distinct from completed acts.
- Avoid duplicate paragraphs. Each planned paragraph must have a distinct pleading purpose.
- The final drafting model will number the paragraphs deterministically; therefore DO NOT put paragraph numbers in purposes.
- The prayer must contain only the reliefs supported by the supplied facts.
- Verification may be included only when the facts identify the person who can verify.

Return only the supplied JSON schema."""


def plan_structure(facts: dict[str, Any], retrieval_context: str) -> dict[str, Any]:
    client = _azure_client()
    payload = {
        "case_facts": facts,
        "advocate_corpus_reference": retrieval_context,
        "instruction": "Create the case-specific pleading architecture now. Do not write the final paragraphs."
    }
    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "dava_structure", "strict": True, "schema": json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))},
        },
        temperature=0,
    )
    plan = json.loads(response.choices[0].message.content)
    return normalize_structure(plan, facts)


def normalize_structure(plan: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministically remove unsafe/duplicated planner output."""
    paragraphs = []
    seen = set()
    for item in plan.get("paragraphs", []):
        purpose = str(item.get("purpose", "")).strip()
        section = str(item.get("section", "other")).strip()
        key = (section, purpose.casefold())
        if not purpose or key in seen:
            continue
        seen.add(key)
        item = dict(item)
        item["source_fields"] = [str(x) for x in item.get("source_fields", []) if str(x).strip()]
        paragraphs.append(item)

    # Never allow unsupported technical sections to be planned as facts.
    available = facts
    def present(k: str) -> bool:
        v = available.get(k)
        return bool(v not in (None, "", [], {}))

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

    # The planner may select a natural title, but never leave a generic empty title.
    title = str(plan.get("title", "")).strip() or "वादपत्र"
    opening = str(plan.get("opening", "")).strip() or "वादी निम्नलिखित निवेदन करता है:-"

    return {
        "document_type": "dava_plaint",
        "nature": str(plan.get("nature", "civil plaint")).strip(),
        "title": title,
        "opening": opening,
        "paragraphs": filtered,
        "prayer_items": [str(x).strip() for x in plan.get("prayer_items", []) if str(x).strip()],
        "include_verification": bool(plan.get("include_verification", False)),
        "warnings": [str(x).strip() for x in plan.get("warnings", []) if str(x).strip()],
    }
