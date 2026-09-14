# Image-to-Draft patch manifest

## Replace
- `app/bot.py`
- `app/drafting_engine/case_intake.py`
- `app/drafting_engine/draft_editor.py`
- `requirements.txt`
- `.env.example`

## Add
- `app/drafting_engine/document_intelligence.py`
- `IMAGE_INPUT.md`
- `tests/test_document_intelligence.py`

This is based on the authorized/editor baseline and includes the corrected
labelled intake parser and draft persistence fix. The new image feature calls
Azure Document Intelligence `prebuilt-layout` directly from code.
