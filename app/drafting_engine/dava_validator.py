"""Deterministic safety and structure checks for AI-generated Dava drafts."""
from __future__ import annotations
import re
from typing import Any

FORBIDDEN_HEADINGS = {"वादी का परिचय", "प्रतिवादी का परिचय", "विवादित संपत्ति", "वाद के तथ्य"}
COMPLETED_POSITIVE = re.compile(r"(?:कब्जा कर लिया|कब्जा किया है|कब्जे में ले लिया|बेदखल कर दिया)")
ATTEMPTED = re.compile(r"(?:कब्जा करने का प्रयास|कब्जा करने का प्रयत्न|कब्जा करने की कोशिश)")
THREAT = re.compile(r"(?:धमकी|धमकाया|धमकी दी)")
NUMBERED_PREFIX = re.compile(r"^\s*(?:\d+\s*(?:\([^)]*\))?|\([क-ह]\)|\([0-9]+\))\s*[:.)-]")


def _all_text(draft: dict[str, Any]) -> str:
    chunks: list[str] = []
    for value in draft.values():
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, list):
            chunks.extend(str(x) for x in value)
    return "\n".join(chunks)


def validate_dava_draft(draft: dict[str, Any], facts: dict[str, Any], structure: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    text = _all_text(draft)

    for heading in FORBIDDEN_HEADINGS:
        if re.search(rf"(?m)^\s*{re.escape(heading)}\s*$", text):
            errors.append(f"generic_metadata_heading:{heading}")

    source = _all_text(facts)
    if ATTEMPTED.search(source) and COMPLETED_POSITIVE.search(text):
        errors.append("event_status_upgrade:attempted_to_completed")

    pleadings = [str(x).strip() for x in draft.get("pleadings", []) if str(x).strip()]
    for item in pleadings:
        if NUMBERED_PREFIX.search(item):
            errors.append("model_numbered_paragraph:renderer_should_number")
            break

    # Exact duplicate paragraphs are almost always a planning/drafting failure.
    normalized = [re.sub(r"\s+", "", x) for x in pleadings]
    if len(normalized) != len(set(normalized)):
        errors.append("duplicate_pleading_paragraph")

    if structure:
        expected = len(structure.get("paragraphs", []))
        if expected and len(pleadings) != expected:
            errors.append(f"structure_paragraph_count:{len(pleadings)}_expected_{expected}")

    if not draft.get("prayer") and facts.get("reliefs"):
        errors.append("missing_prayer")
    if not str(draft.get("title", "")).strip() or str(draft.get("title", "")).strip() == "वाद पत्र":
        errors.append("generic_title_only")
    return errors
