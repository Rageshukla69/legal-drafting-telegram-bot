# Post-Draft Editor

The bot now supports a conservative post-draft editing mode. After a draft is generated, the user can press **Edit Draft** and send a Hindi/English text instruction or a voice note.

The AI only parses the instruction into one structured operation. Python then applies that operation to the stored structured draft and re-renders DOCX/PDF. A confirmation button is shown before applying the change.

Supported operations:
- replace exact text inside a paragraph/section
- delete exact text inside a paragraph/section
- insert before a selected paragraph/item
- insert after a selected paragraph/item

The original structured draft is retained in the case state, and up to 10 versions are retained for rollback/history. This is intentionally not a PDF/OCR editor.
