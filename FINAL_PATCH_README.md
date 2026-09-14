# Final Semantic Intake + Image/PDF + Editor Patch

This patch is intended for the current authorized seven-draft bot baseline.

## Replace
- app/bot.py
- app/drafting_engine/case_intake.py
- app/drafting_engine/conversation_state.py
- app/drafting_engine/intake_schema.json
- app/drafting_engine/multi_draft_orchestrator.py
- app/drafting_engine/document_intelligence.py
- app/drafting_engine/draft_editor.py
- requirements.txt
- .env.example

## Add
- tests/test_final_intake_pipeline.py
- tests/test_document_intelligence.py
- FINAL_PATCH_README.md

## Important
The semantic intake no longer depends on labels. It uses Gemini to classify natural legal narrative by meaning, uses the entire user-message history, preserves current accepted facts, and only asks for fields still genuinely absent. Explicit `Label: value` blocks remain as a deterministic safety override.

Image/PDF/DOCX-capable Document Intelligence is called directly through Azure Document Intelligence v4 REST (`2024-11-30`) with `prebuilt-layout`; no extra Azure Document Intelligence SDK is required.

Existing Telegram voice remains on the existing Azure Speech path. The post-draft editor remains available and stores up to 10 structured versions.

Do not commit or include real API keys.
