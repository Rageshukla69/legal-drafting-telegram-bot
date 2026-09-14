# Seven supported draft types

The Telegram UI routes all seven corpus document types through the same structured intake → type-specific corpus retrieval → Gemini drafting → deterministic DOCX/PDF pipeline:

1. Dava / Plaint (`dava_plaint`)
2. Written Statement (`written_statement`)
3. Application (`application`)
4. Evidence / PW Affidavit (`evidence_pw_affidavit`)
5. Affidavit (`affidavit`)
6. Legal Notice (`legal_notice`)
7. Other Civil Draft (`other_civil`)

Each type retrieves only its corresponding `document_type` from `corpus_index.json` and uses its corresponding specialist prompt in `prompts/`.
