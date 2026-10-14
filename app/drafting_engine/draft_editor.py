from __future__ import annotations
import json, os, re
from copy import deepcopy
from typing import Any
from .gemini_client import GeminiClient
from .krutidev_converter import sanitize_draft_unicode

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": [
                "replace",
                "replace_all",
                "delete",
                "insert_before",
                "insert_after",
                "append",
                "page_break_before",
                "page_break_after",
                "remove_page_break_before",
                "remove_page_break_after",
            ],
        },
        "section": {
            "type": "string",
            "enum": [
                "pleadings",
                "prayer",
                "opening_averment",
                "verification",
                "parties",
                "signature_block",
                "title",
                "court_heading",
                "case_heading",
            ],
        },
        "index": {"type": "integer", "minimum": 0},
        "old_text": {"type": "string"},
        "new_text": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["operation", "section", "index", "old_text", "new_text", "reason"],
}

SYSTEM = """You are a precise legal-document edit parser for an Indian advocate's Hindi drafting bot.
Convert the lawyer's natural-language or voice-dictated edit instruction into a structured edit operation against the supplied structured draft.

Rules:
- Never rewrite unrelated parts of the document.
- Select the target `section` and zero-based `index` (use 0 for scalar sections or when using `append` / `replace_all`).
- Supported text operations:
  * `replace`: replace `old_text` with `new_text` in the selected section/index. `old_text` MUST be an exact contiguous substring of the selected item.
  * `replace_all`: when the lawyer wants a name, Gata/Khasra number, date, or term corrected everywhere it appears across the entire document, set `operation="replace_all"`, `old_text` to the exact existing text, and `new_text` to the replacement text.
  * `delete`: delete `old_text` from the selected item (or if `old_text` equals the entire item in a list section, remove that item).
  * `insert_before` / `insert_after`: insert `new_text` before or after `index` in a list section (`pleadings`, `prayer`, `parties`, `signature_block`), or anchored by `old_text` in a scalar section.
  * `append`: add a new paragraph/clause `new_text` to the end of `pleadings`, `prayer`, `parties`, or `signature_block` (`old_text` should be empty).
- Supported layout operations: `page_break_before`, `page_break_after`, `remove_page_break_before`, `remove_page_break_after`.
  * Requests like “प्रार्थना दूसरे पेज पर ले जाओ”, “prayer next page”, “सत्यापन नए पेज से शुरू करो” mean `page_break_before` on `section=prayer` or `section=verification` with empty `old_text` and `new_text`.
- Always write Hindi legal additions/replacements in clean Unicode Devanagari Hindi (starting new pleading paragraphs with "यह कि " when appropriate, without numbering).
- Return JSON only matching the schema.
"""


def _flex_replace_once(source: str, old: str, new: str) -> str | None:
    """Replace `old` in `source`, falling back to whitespace-flexible matching."""
    if old in source:
        return source.replace(old, new, 1)
    tokens = [re.escape(tok) for tok in old.strip().split() if tok]
    if not tokens:
        return None
    pattern = re.compile(r"\s+".join(tokens))
    m = pattern.search(source)
    if m:
        return source[:m.start()] + new + source[m.end():]
    return None


class DraftEditor:
    def __init__(self):
        self.client = None

    def _get_client(self) -> GeminiClient:
        if self.client is None:
            self.client = GeminiClient()
        return self.client

    @staticmethod
    def _sections(draft: dict[str, Any]) -> dict[str, list[str]]:
        sections = {}
        for key in ("pleadings", "prayer", "parties", "signature_block"):
            value = draft.get(key, [])
            if not isinstance(value, list):
                value = [value]
            sections[key] = [str(x) for x in value]
        for key in ("opening_averment", "verification", "title", "court_heading", "case_heading"):
            value = str(draft.get(key, "") or "")
            sections[key] = [value] if value else []
        return sections

    @staticmethod
    def _deterministic_layout_intent(instruction: str) -> dict[str, Any] | None:
        """Handle unambiguous page-layout requests without depending on LLM wording."""
        text = re.sub(r"\s+", " ", str(instruction or "").strip().casefold())
        if not text:
            return None

        next_page = any(marker in text for marker in (
            "दूसरे पेज", "दूसरे पृष्ठ", "दूसरे पन्ने", "अगले पेज", "अगले पृष्ठ",
            "next page", "second page", "page 2", "page-2", "new page", "नए पेज", "नए पृष्ठ",
        ))
        if not next_page:
            return None

        section = None
        if any(marker in text for marker in ("प्रार्थना", "राहत", "prayer", "relief")):
            section = "prayer"
        elif any(marker in text for marker in ("सत्यापन", "verification", "verify")):
            section = "verification"
        elif any(marker in text for marker in ("हस्ताक्षर", "सिग्नेचर", "signature")):
            section = "signature_block"
        elif any(marker in text for marker in ("वाद पत्र", "pleadings", "pleading", "paragraph", "पैराग्राफ")):
            section = "pleadings"
        elif any(marker in text for marker in ("शीर्षक", "title", "heading")):
            section = "title"

        if not section:
            return None

        return {
            "operation": "page_break_before",
            "section": section,
            "index": 0,
            "old_text": "",
            "new_text": "",
            "reason": "Unambiguous natural-language request to start the selected section on the next page.",
        }

    def parse(self, draft: dict[str, Any], instruction: str) -> dict[str, Any]:
        direct = self._deterministic_layout_intent(instruction)
        if direct:
            return direct
        context = json.dumps({"draft": draft, "editable_sections": self._sections(draft)}, ensure_ascii=False, indent=2)
        res = self._get_client().generate_json(
            system=SYSTEM,
            prompt=f"USER EDIT INSTRUCTION:\n{instruction.strip()}\n\nCURRENT DRAFT:\n{context}",
            schema=EDIT_SCHEMA,
            thinking_level=os.getenv("GEMINI_EDIT_THINKING_LEVEL", "medium"),
        )
        return sanitize_draft_unicode(res)

    @staticmethod
    def _valid_section(draft, section):
        return section in {
            "pleadings", "prayer", "opening_averment", "verification",
            "parties", "signature_block", "title", "court_heading", "case_heading",
        }

    def apply(self, draft: dict[str, Any], edit: dict[str, Any]) -> tuple[dict[str, Any], str]:
        out = deepcopy(draft)
        section = str(edit.get("section", ""))
        operation = str(edit.get("operation", ""))
        old = str(edit.get("old_text", ""))
        new = str(edit.get("new_text", ""))
        index = int(edit.get("index", -1))
        if not self._valid_section(out, section):
            raise ValueError("Invalid edit section.")
        layout_ops = {"page_break_before", "page_break_after", "remove_page_break_before", "remove_page_break_after"}
        text_ops = {"replace", "replace_all", "delete", "insert_before", "insert_after", "append"}
        if operation not in text_ops | layout_ops:
            raise ValueError("Unsupported edit operation.")

        if operation in layout_ops:
            if old or new:
                raise ValueError("Layout edits must not contain old_text/new_text.")
            layout = out.setdefault("layout", {})
            if not isinstance(layout, dict):
                layout = {}
                out["layout"] = layout
            key = "page_break_before" if "before" in operation else "page_break_after"
            values = layout.get(key, [])
            if not isinstance(values, list):
                values = [values]
            allowed = {
                "court_heading", "case_heading", "parties", "title",
                "opening_averment", "pleadings", "prayer", "signature_block", "verification",
            }
            values = [str(x) for x in values if str(x) in allowed]
            if operation.startswith("remove_"):
                values = [x for x in values if x != section]
            elif section not in values:
                values.append(section)
            layout[key] = values
            return out, self.describe(edit)

        if operation == "replace_all":
            if not old:
                raise ValueError("replace_all requires old_text.")
            changed = 0
            for sec_key in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
                val = str(out.get(sec_key, "") or "")
                if old in val:
                    out[sec_key] = val.replace(old, new)
                    changed += 1
            for list_key in ("parties", "pleadings", "prayer", "signature_block"):
                items = out.get(list_key, [])
                if isinstance(items, list):
                    new_items = []
                    for it in items:
                        s_it = str(it)
                        if old in s_it:
                            s_it = s_it.replace(old, new)
                            changed += 1
                        new_items.append(s_it)
                    out[list_key] = new_items
            if not changed:
                raise ValueError("The target text was not found in the draft; no change applied.")
            return sanitize_draft_unicode(out), self.describe(edit)

        scalar = section in {"opening_averment", "verification", "title", "court_heading", "case_heading"}
        if scalar:
            current = str(out.get(section, "") or "")
            if index not in (0, -1):
                raise ValueError("Invalid target index.")
            if not current:
                if operation in {"replace", "append", "insert_after"} and new:
                    out[section] = new
                    return sanitize_draft_unicode(out), self.describe(edit)
                raise ValueError("Target section is empty.")
            if operation in {"replace", "delete"}:
                if not old:
                    raise ValueError("The exact target text was not found; no change applied.")
                replacement = "" if operation == "delete" else new
                updated = _flex_replace_once(current, old, replacement)
                if updated is None:
                    raise ValueError("The exact target text was not found; no change applied.")
                out[section] = updated
            elif operation == "append":
                out[section] = (current + " " + new).strip()
            else:
                if not old or old not in current:
                    raise ValueError("The exact anchor text was not found; no change applied.")
                repl = (new + old) if operation == "insert_before" else (old + new)
                out[section] = current.replace(old, repl, 1)
        else:
            items = out.get(section, [])
            if not isinstance(items, list):
                items = [items]
            if operation == "append":
                if not new.strip():
                    raise ValueError("Cannot append empty text.")
                items.append(new.strip())
                out[section] = items
                return sanitize_draft_unicode(out), self.describe(edit)
            if index < 0 or index >= len(items):
                raise ValueError("Target paragraph/item index is out of range.")
            current = str(items[index])
            if operation in {"replace", "delete"}:
                if not old:
                    raise ValueError("The exact target text was not found; no change applied.")
                replacement = "" if operation == "delete" else new
                updated = _flex_replace_once(current, old, replacement)
                if updated is None:
                    # Fallback: search across other items in the same section if the lawyer gave 1-based vs 0-based index
                    found_idx = next((i for i, it in enumerate(items) if _flex_replace_once(str(it), old, replacement) is not None), None)
                    if found_idx is None:
                        raise ValueError("The exact target text was not found; no change applied.")
                    index = found_idx
                    edit["index"] = found_idx
                    updated = _flex_replace_once(str(items[found_idx]), old, replacement)
                if operation == "delete" and not updated.strip():
                    items.pop(index)
                else:
                    items[index] = updated.strip()
            elif operation == "insert_before":
                if old and old not in current:
                    raise ValueError("The exact anchor text was not found; no change applied.")
                items.insert(index, new)
            elif operation == "insert_after":
                if old and old not in current:
                    raise ValueError("The exact anchor text was not found; no change applied.")
                items.insert(index + 1, new)
            out[section] = items
        return sanitize_draft_unicode(out), self.describe(edit)

    @staticmethod
    def describe(edit: dict[str, Any]) -> str:
        op = edit.get("operation")
        section = edit.get("section")
        idx = edit.get("index", 0)
        old = str(edit.get("old_text", ""))
        new = str(edit.get("new_text", ""))
        labels = {
            "pleadings": "पैराग्राफ",
            "prayer": "प्रार्थना",
            "parties": "पक्षकार",
            "signature_block": "हस्ताक्षर/सिग्नेचर",
            "opening_averment": "प्रारंभिक कथन",
            "verification": "सत्यापन",
            "title": "शीर्षक",
            "court_heading": "न्यायालय शीर्षक",
            "case_heading": "केस शीर्षक",
        }
        target = labels.get(section, section)
        if section in {"pleadings", "prayer", "parties", "signature_block"} and op != "append":
            target += f" {int(idx) + 1}"
        if op == "page_break_before":
            return f"📄 {target} को नए पेज से शुरू किया जाएगा।"
        if op == "page_break_after":
            return f"📄 {target} के बाद नया पेज शुरू किया जाएगा।"
        if op == "remove_page_break_before":
            return f"↩️ {target} से पहले का page break हटाया जाएगा।"
        if op == "remove_page_break_after":
            return f"↩️ {target} के बाद का page break हटाया जाएगा।"
        if op == "replace_all":
            return f"✏️ पूरे ड्राफ्ट में सभी स्थानों पर बदलाव:\nBefore: {old}\nAfter: {new}"
        if op == "append":
            return f"➕ {target} के अंत में नया बिंदु जोड़ा जाएगा:\n{new}"
        if op == "delete":
            return f"🗑️ {target} से यह अंश हटेगा:\n{old}"
        if op == "replace":
            return f"✏️ {target} में बदलाव:\nBefore: {old}\nAfter: {new}"
        if op == "insert_before":
            return f"➕ {target} से पहले जोड़ा जाएगा:\n{new}"
        return f"➕ {target} के बाद जोड़ा जाएगा:\n{new}"
