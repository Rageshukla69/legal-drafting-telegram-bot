# Final Reliability Patch — Semantic Intake + Image/PDF + Draft Editor

This patch is for the authorized seven-draft Telegram bot baseline.

## Replace
- `app/bot.py`
- `app/drafting_engine/case_intake.py`
- `app/drafting_engine/conversation_state.py`
- `app/drafting_engine/intake_schema.json`
- `app/drafting_engine/multi_draft_orchestrator.py`
- `app/drafting_engine/document_intelligence.py`
- `app/drafting_engine/draft_editor.py`
- `requirements.txt`
- `.env.example`

## Add
- `tests/test_semantic_intake_regression.py`
- `tests/test_document_intelligence.py`
- `FINAL_PATCH_README.md`

## What this fixes

The intake engine no longer depends on a fixed wording or on headings such as `Facts:` or `Relief:`.
It uses Gemini semantic extraction over accumulated user-authored messages. It then performs a separate
completeness audit whenever required information may be missing. The audit is specifically instructed to
recover narrative/chronological/legal statements that a first pass omitted or classified incorrectly.

A deterministic labelled parser remains only as a safety override for explicit `Label: value` blocks. A small
narrative/request rescue is also used only when AI extraction leaves facts or relief empty; it preserves the
user's text and does not invent parties, dates, property, statutes, etc.

Immediately before `Generate Draft`, the bot re-runs the accumulated user evidence through the same semantic
reconciliation path. This is important for cases that were collected before the patch was deployed or where a
previous turn was incompletely classified.

## Input support

- Normal text
- Existing Telegram voice -> existing Azure Speech -> semantic intake
- Telegram photos/images -> Azure Document Intelligence `prebuilt-layout` -> semantic intake
- Telegram PDF and supported document uploads -> Azure Document Intelligence `prebuilt-layout` -> semantic intake

The Document Intelligence integration uses the v4 REST API directly and does not add another Azure SDK.

## Environment variables

Add these new variables in Heroku:

- `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT`
- `AZURE_DOCUMENT_INTELLIGENCE_KEY`
- `AZURE_DOCUMENT_INTELLIGENCE_MODEL=prebuilt-layout`
- `AZURE_DOCUMENT_INTELLIGENCE_API_VERSION=2024-11-30`
- `AZURE_DOCUMENT_INTELLIGENCE_POLL_SECONDS=1.5`
- `AZURE_DOCUMENT_INTELLIGENCE_TIMEOUT_SECONDS=600`
- `IMAGE_INTAKE_CHUNK_CHARS=45000`
- `IMAGE_INTAKE_MAX_CHUNKS=40`

Optional intake tuning:

- `GEMINI_INTAKE_HISTORY_MESSAGES=40`
- `GEMINI_INTAKE_HISTORY_CHARS=80000`
- `GEMINI_INTAKE_AUDIT_ALWAYS=1`

## Important

This improves robustness substantially, but no AI system can guarantee perfect legal interpretation. The system is
therefore deliberately conservative: it may ask for genuinely ambiguous information instead of inventing it.
The 306 advocate drafts remain style/structure references, not sources of case facts.

Do not commit API keys.
