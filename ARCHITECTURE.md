# Current architecture

Telegram
  ↓
app/bot.py
  ↓
case_store.py
  ↓
Phase 4C Dava engine
  ├── conversation_state
  ├── question_generator
  ├── retriever
  ├── Dava composer
  └── Azure Structured Outputs adapter

Next:
  Azure natural-language fact extraction
  ↓
  validation
  ↓
  deterministic DOCX renderer
  ↓
  Telegram document reply

Later:
  voice → speech-to-text
  photo/PDF → OCR/layout extraction
  document classifier
  Written Statement / Applications / Affidavits / Notices / Other Civil
