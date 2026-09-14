# PATCH MANIFEST

This ZIP is a replacement patch for the authorized seven-draft bot with the post-draft editor and image/PDF intake.

## Replace
- app/bot.py
- app/drafting_engine/case_intake.py
- app/drafting_engine/conversation_state.py
- app/drafting_engine/document_intelligence.py
- app/drafting_engine/draft_editor.py
- app/drafting_engine/intake_schema.json
- app/drafting_engine/multi_draft_orchestrator.py
- requirements.txt
- .env.example

## Add
- tests/test_semantic_intake_regression.py
- tests/test_document_intelligence.py
- FINAL_PATCH_README.md
- PATCH_MANIFEST.md

## Key reliability changes
1. Semantic extraction does not depend on labels or fixed wording.
2. Full accumulated user evidence is reviewed.
3. A second independent completeness audit recovers omitted facts/reliefs.
4. Generate Draft performs a final reconciliation before checking missing fields.
5. Deterministic label parsing and narrative/request rescue are safety nets only.
6. Existing voice remains on Azure Speech.
7. Photos and PDFs use Azure Document Intelligence `prebuilt-layout`.
8. Draft editor remains included.
9. Draft version counter is no longer incremented twice on initial generation.
