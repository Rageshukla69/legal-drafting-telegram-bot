#!/usr/bin/env python3
"""Build Phase 8 corpus index from extracted category folders."""
from pathlib import Path
import sys

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "legal_corpus_extracted")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "corpus_index/documents.json")

from app.drafting_engine.corpus_manager import build_index, save_index

index = build_index(ROOT)
save_index(index, OUT)
print(f"Indexed {len(index['documents'])} documents -> {OUT}")
