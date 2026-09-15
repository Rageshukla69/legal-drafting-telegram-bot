# Hindi Rendering + Legacy Corpus Safety Patch

## What this fixes

### 1. PDF Devanagari rendering
The PDF renderer previously switched fonts at individual characters. That can break complex-script shaping and produce cramped or malformed Hindi glyphs.

The patch now:
- keeps contiguous Devanagari text in one shaping run;
- routes only real Latin/numeric runs to the Latin fallback font;
- keeps ordinary punctuation with the surrounding script;
- removes the accidental duplicate character append in the old PDF markup path;
- pins ReportLab to `>=4.4,<5` and keeps `uharfbuzz` installed for South-Asian shaping.

### 2. Legacy/partially-converted corpus poisoning
The supplied `corpus_index.json` contains mixed legacy-font artifacts inside Hindi text. Examples include strings such as `mपराsDत`, `जwनियर`, and `डिवhजन`. Sending these bodies to Gemini can cause the model to reproduce corrupted legal terminology.

The patch therefore defaults to **safe corpus mode**:
- retrieval still uses the 306-document index for document type/structure ranking;
- retrieved body text is **not** sent to Gemini by default;
- a clean Unicode style anchor is supplied to the final drafting engine;
- generated drafts are rejected and regenerated if they contain strong mixed-font corruption signals;
- `CORPUS_ALLOW_LEGACY_TEXT=false` is the default.

Only set `CORPUS_ALLOW_LEGACY_TEXT=true` after the corpus has been fully re-OCR'd/reindexed from clean source documents.

## Files

- `app/drafting_engine/renderers/legal_document_renderer.py`
- `app/drafting_engine/hindi_corpus_guard.py`
- `app/drafting_engine/hybrid_retriever.py`
- `app/drafting_engine/multi_draft_orchestrator.py`
- `app/drafting_engine/style_reference.txt`
- `requirements.txt`
- `.env.example`
- regression tests under `tests/`

## Verification

The targeted regression suite passes, including PDF shaping, Unicode fallback, number formatting, and safe corpus retrieval tests.
