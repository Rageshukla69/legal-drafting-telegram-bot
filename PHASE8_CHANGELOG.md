# Phase 8.0 — Corpus Intelligence Foundation

- Treat the seven Drive/GitHub category folders as ground-truth document types.
- Add DOCX/PDF extraction and structural signal indexing.
- Add lightweight category-aware retrieval foundation.
- Add dedicated specialist prompt templates for every legal category.
- Do not alter the live Telegram bot yet.
- Preserve the separation: user facts vs advocate corpus vs legal authority.

Next: Phase 8.1 will connect the corpus index/retrieval and a dedicated document-classification AI to the Telegram intake pipeline, after the index has been inspected.

## Multi-document case extension
- Added persistent registry of multiple generated document types per case.
- Added natural-language text/voice detection for explicit requests to create another document.
- Added **Add Another Document** and **Case Documents** UI.
- Shared case facts are retained when switching from Dava to affidavit, written statement, application, evidence affidavit, legal notice, or other civil draft.
- Each generated document retains its own structured draft and edit history.
