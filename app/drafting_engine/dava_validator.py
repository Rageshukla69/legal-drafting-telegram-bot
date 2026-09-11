"""Deterministic safety/quality checks for generated Dava drafts."""
from __future__ import annotations
import re
from typing import Any

HEADING_FORBIDDEN = {
    "वादी का परिचय", "प्रतिवादी का परिचय", "विवादित संपत्ति", "वाद के तथ्य"
}

COMPLETED_POSITIVE = re.compile(r"(?:कब्जा कर लिया|कब्जा किया है|कब्जे में ले लिया)")
ATTEMPTED = re.compile(r"(?:कब्जा करने का प्रयास|कब्जा करने का प्रयत्न|कब्जा करने की कोशिश)")


def _all_text(draft: dict[str, Any]) -> str:
    chunks = []
    for key, value in draft.items():
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, list):
            chunks.extend(str(x) for x in value)
    return "\n".join(chunks)


def validate_dava_draft(draft: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    text = _all_text(draft)

    for heading in HEADING_FORBIDDEN:
        if re.search(rf"(?m)^\s*{re.escape(heading)}\s*$", text):
            errors.append(f"generic_metadata_heading:{heading}")

    source = _all_text(facts)
    if ATTEMPTED.search(source) and COMPLETED_POSITIVE.search(text):
        errors.append("event_status_upgrade:attempted_to_completed")

    # A requested relief should not appear as a standalone numbered pleading.
    reliefs = [str(x).strip() for x in facts.get("reliefs", []) if str(x).strip()]
    pleading_text = "\n".join(str(x) for x in draft.get("pleadings", []))
    for relief in reliefs:
        normalized = re.sub(r"\s+", "", relief)
        if normalized and normalized in re.sub(r"\s+", "", pleading_text):
            # This is a warning-level structural check; exact overlap is often legitimate
            # inside a legal conclusion, so only reject if it is an entire pleading item.
            for item in draft.get("pleadings", []):
                if re.sub(r"\s+", "", str(item)) == normalized:
                    errors.append("relief_as_fact")
                    break

    if not draft.get("prayer") and facts.get("reliefs"):
        errors.append("missing_prayer")
    if draft.get("title", "").strip() == "वाद पत्र":
        errors.append("generic_title_only")
    return errors
