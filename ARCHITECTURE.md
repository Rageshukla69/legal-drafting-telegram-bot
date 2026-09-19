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
  canonical DOCX renderer (python-docx)
  ↓
  LibreOffice headless  →  PDF (converted from that DOCX)
  ↓
  Telegram document reply

See DOCX_PDF_PARITY.md for why the PDF is a conversion of the DOCX rather than
a second, independent layout.

Later:
  voice → speech-to-text
  photo/PDF → OCR/layout extraction
  document classifier
  Written Statement / Applications / Affidavits / Notices / Other Civil
