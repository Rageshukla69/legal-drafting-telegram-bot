"""Lightweight corpus retrieval foundation; semantic/vector retrieval comes next."""
from __future__ import annotations

import re
from pathlib import Path

from .corpus_manager import CATEGORIES


def _tokens(text: str) -> set[str]:
    return {x for x in re.findall(r"[\w\u0900-\u097F]+", text.lower()) if len(x) > 2}


def retrieve(index: dict, document_type: str, query: str, top_k: int = 5) -> list[dict]:
    q = _tokens(query)
    wanted_category = next((c for c, t in CATEGORIES.items() if t == document_type), None)
    candidates = [d for d in index.get("documents", []) if wanted_category is None or d["category"] == wanted_category]
    scored = []
    for d in candidates:
        hay = _tokens((d.get("text") or "")[:50000])
        overlap = len(q & hay)
        # Prefer examples with useful legal structure signals.
        s = overlap
        st = d.get("structure", {})
        s += 1 if st.get("contains_prayer") else 0
        s += 1 if st.get("contains_verification") else 0
        s += 1 if st.get("contains_advocate") else 0
        scored.append((s, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:top_k]]
