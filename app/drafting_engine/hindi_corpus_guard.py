"""Protect Gemini from the legacy/partially-converted Hindi corpus.

The bundled 306-document index contains mixed legacy-font artifacts such as
Latin characters embedded inside Devanagari words. Feeding those strings to an
LLM makes the model reproduce corrupted legal terminology. Until the corpus is
fully re-OCR'd/reindexed from clean source documents, this module deliberately
exposes only structural metadata and a small clean style anchor.
"""
from __future__ import annotations

import re
from typing import Any

# Strong evidence that a string is not clean Unicode Hindi. These characters
# occur throughout the legacy-font conversion artifacts in the supplied index.
_LEGACY_MARKERS = set("¼½]@&vksvkDkDmkshwqszZfUaAEIOTRPLKX<>{}~")


def legacy_corruption_score(text: str) -> float:
    s = str(text or "")
    if not s:
        return 0.0
    dev = sum("\u0900" <= c <= "\u097f" for c in s)
    latin = sum("A" <= c <= "z" for c in s)
    markers = sum(c in _LEGACY_MARKERS for c in s)
    # A normal Hindi legal paragraph may contain a few Latin identifiers; the
    # supplied legacy corpus has a much higher mixed-script density.
    return (latin + 1.5 * markers) / max(1, dev)


def is_clean_hindi_reference(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    return legacy_corruption_score(s) < 0.08


def safe_reference_block(result: dict[str, Any], index: int) -> str:
    structure = result.get("structure") or {}
    return (
        f"[ORIGINAL DRAFT STRUCTURE {index}]\n"
        f"filename: {result.get('filename', '')}\n"
        f"category: {result.get('category', '')}\n"
        "ROLE: STRUCTURE/METADATA REFERENCE ONLY. The indexed body text is intentionally omitted because the supplied legacy corpus contains mixed-font encoding artifacts.\n"
        f"structure: {structure}"
    )
