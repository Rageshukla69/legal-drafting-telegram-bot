"""Deterministic quality/safety validator for Dava drafts."""
from __future__ import annotations
import re
from typing import Any

FORBIDDEN_HEADINGS = {"वादी का परिचय", "प्रतिवादी का परिचय", "विवादित संपत्ति", "वाद के तथ्य"}
COMPLETED_POSITIVE = re.compile(r"(?:कब्जा कर लिया|कब्जा किया है|कब्जे में ले लिया|बेदखल कर दिया|कब्जा हो गया)")
ATTEMPTED = re.compile(r"(?:कब्जा करने का प्रयास|कब्जा करने का प्रयत्न|कब्जा करने की कोशिश|कब्जा करने की धमकी|कब्जा करने का प्रयास किया)")
NUMBERED_PREFIX = re.compile(r"^\s*(?:\d+\s*(?:\([^)]*\))?|\([क-ह]\)|\([0-9]+\))\s*[:.)-]")


def _all_text(value: Any) -> str:
    if isinstance(value, str): return value
    if isinstance(value, list): return "\n".join(_all_text(x) for x in value)
    if isinstance(value, dict): return "\n".join(_all_text(v) for v in value.values())
    return str(value) if value is not None else ""


def _fact_text(facts: dict[str, Any]) -> str:
    return _all_text(facts)


def validate_dava_draft(draft: dict[str, Any], facts: dict[str, Any], structure: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    all_text = _all_text(draft)
    source = _fact_text(facts)

    for heading in FORBIDDEN_HEADINGS:
        if re.search(rf"(?m)^\s*{re.escape(heading)}\s*:??\s*$", all_text):
            errors.append(f"generic_metadata_heading:{heading}")

    if ATTEMPTED.search(source) and COMPLETED_POSITIVE.search(all_text):
        errors.append("event_status_upgrade:attempted_to_completed")

    pleadings = [str(x).strip() for x in draft.get("pleadings", []) if str(x).strip()]
    if not pleadings:
        errors.append("missing_pleadings")
    for p in pleadings:
        if NUMBERED_PREFIX.search(p):
            errors.append("model_numbered_paragraph:renderer_should_number")
            break

    normalized = [re.sub(r"\s+", "", x).casefold() for x in pleadings]
    if len(normalized) != len(set(normalized)):
        errors.append("duplicate_pleading_paragraph")

    if structure:
        expected = len(structure.get("paragraphs", []))
        if len(pleadings) != expected:
            errors.append(f"structure_paragraph_count:{len(pleadings)}_expected_{expected}")
        plan_sections = [str(x.get("section", "")) for x in structure.get("paragraphs", [])]
        if any(s == "intro" for s in plan_sections):
            errors.append("structure_contains_party_intro_paragraph")

    title = str(draft.get("title", "")).strip()
    if not title:
        errors.append("missing_title")
    if "वादी का परिचय" in title or "प्रतिवादी का परिचय" in title:
        errors.append("bad_title_metadata_style")

    if not draft.get("prayer"):
        errors.append("missing_prayer")
    if not str(draft.get("verification", "")).strip():
        errors.append("missing_verification")

    return errors
