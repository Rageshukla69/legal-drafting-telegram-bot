# Patch: ABCD/Unicode glyphs + Indian legal number convention

Base this patch on:
`legal-drafting-telegram-bot-multi-document-layout-edit-unicode-fixed.zip`

## Files to replace/add

### Replace
- `app/drafting_engine/renderers/legal_document_renderer.py`

### Add
- `app/drafting_engine/legal_number_formatter.py`
- `tests/test_legal_number_formatter.py`
- `tests/test_ascii_font_routing.py`

## What this patch fixes

### 1. ABCD / Latin glyph boxes
ASCII letters, digits and punctuation are explicitly routed to the bundled `Noto Sans` Latin font instead of relying on PDF/Word viewer fallback. This is intended to stop map labels such as `A`, `B`, `C`, `D` from becoming empty/tofu boxes.

### 2. Legal numeric convention
At the final rendering boundary, important numeric legal facts are deterministically written in the corpus-style form:

- `526` in a property/legal-number context -> `526 (पाँच सौ छब्बीस)`
- `7 डिसमिल` -> `7 (सात) डिसमिल`
- `80 वर्ष` -> `80 (अस्सी) वर्ष`
- `10,000 रुपये` -> `10,000 (दस हजार) रुपये`
- `24.06.1969` -> `24.06.1969 (चौबीस जून उन्नीस सौ उनहत्तर)`

The formatter is intentionally **contextual**. It does NOT blindly expand every number. It protects phone numbers, PIN/Aadhaar-like values, case/document IDs, URLs, alphanumeric identifiers and similar technical identifiers.

Formatting is applied only when rendering DOCX/PDF, so the authoritative structured `CaseState` draft is not mutated. Re-rendering is idempotent and will not create nested parentheses.

## Validation

Targeted regression tests pass:

`7 passed`

The complete repository test run still has two unrelated baseline/environment failures in this environment:
- `tests/phase7_2_smoke_test.py` has an existing assertion mismatch.
- `tests/test_multi_document_case.py` requires the Telegram dependency, which is not installed in this execution environment.

## Important

Do not replace `.env` or the corpus. Keep the existing Unicode fonts from the previous Unicode-fix patch, especially:
- `app/drafting_engine/assets/fonts/NotoSans-Regular.ttf`
- `app/drafting_engine/assets/fonts/DejaVuSans.ttf`
- `app/drafting_engine/assets/fonts/NotoSansDevanagari-Regular.ttf`
- `app/drafting_engine/assets/fonts/NotoSansDevanagari-Bold.ttf`
