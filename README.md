# Phase 8 — Corpus Intelligence Foundation

This phase changes the drafting architecture from template-driven to corpus-driven.

The repository's `legal_corpus/` category ZIPs are treated as the advocate's source corpus. The category names are ground truth. The system extracts document text and structural signals, then builds a searchable JSON index for later semantic retrieval.

## Current corpus categories
- 01_Vaad_Patra_Dava
- 02_Jawab_Dava_WS
- 03_Applications_PrarthanaPatra
- 04_Evidence_PW_Affidavits
- 05_ShapathPatra_Affidavits
- 06_Legal_Notices
- 08_Other_Civil_Drafts

## Design rule
User facts are authoritative for facts. The corpus is authoritative only for drafting patterns, structure, terminology, and style. Legal authorities are a separate source.

## Files
- `app/drafting_engine/corpus_manager.py` — downloads/extracts corpus ZIPs and builds a document index.
- `app/drafting_engine/corpus_retriever.py` — retrieves category- and keyword-relevant examples without requiring a vector database yet.
- `app/drafting_engine/corpus_prompts.py` — dedicated specialist prompt construction.
- `prompts/*.txt` — category-specific specialist instructions.
- `scripts/build_corpus_index.py` — one-shot index builder for local/Heroku execution.
- `tests/phase8_smoke_test.py` — offline test.

## Important
This phase does NOT change the existing Telegram bot flow yet. First build and inspect the corpus index. Then Phase 8.1 can wire classification + retrieval into the bot safely.
