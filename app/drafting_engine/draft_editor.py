from __future__ import annotations
import json, os, re
from copy import deepcopy
from typing import Any
from .gemini_client import GeminiClient

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type":"string", "enum":["replace","delete","insert_before","insert_after","page_break_before","page_break_after","remove_page_break_before","remove_page_break_after"]},
        "section": {"type":"string", "enum":["pleadings","prayer","opening_averment","verification","parties","signature_block","title","court_heading","case_heading"]},
        "index": {"type":"integer", "minimum":0},
        "old_text": {"type":"string"},
        "new_text": {"type":"string"},
        "reason": {"type":"string"}
    },
    "required":["operation","section","index","old_text","new_text","reason"]
}

SYSTEM = """You are a conservative legal-document edit parser. Convert a lawyer's natural-language edit instruction into ONE precise edit operation against the supplied structured draft.
Rules:
- Never rewrite the whole document.
- Select exactly one section and zero-based index.
- Supported text edits are replace/delete/insert_before/insert_after.
- For replace/delete, old_text MUST be an exact contiguous substring of the selected item.
- For insert_before/insert_after, old_text identifies the exact anchor item/text and new_text is the inserted text.
- Supported layout edits are page_break_before/page_break_after/remove_page_break_before/remove_page_break_after.
- A request such as “प्रार्थना दूसरे पेज पर ले जाओ”, “prayer next page”, “प्रार्थना नए पेज से शुरू हो”, or “प्रार्थना को page 2 पर रखो” means page_break_before on section=prayer. It is a layout edit, not a text edit.
- For layout edits, old_text and new_text MUST be empty. The renderer will place the selected section at a new page boundary.
- Preserve every character outside the requested target.
- Do not invent legal facts. Only use text explicitly requested by the user.
- If the user asks to change multiple places, return the first precise operation only; explain in reason that one operation is safer.
- For a request that is ambiguous, choose no speculative target: return old_text as empty and explain the ambiguity in reason.
- Return JSON only.
"""

class DraftEditor:
    def __init__(self):
        self.client = GeminiClient()

    @staticmethod
    def _sections(draft: dict[str, Any]) -> dict[str, list[str]]:
        sections = {}
        for key in ("pleadings","prayer","parties","signature_block"):
            value = draft.get(key, [])
            if not isinstance(value, list): value = [value]
            sections[key] = [str(x) for x in value]
        for key in ("opening_averment","verification","title","court_heading","case_heading"):
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
        context = json.dumps({"draft":draft,"editable_sections":self._sections(draft)}, ensure_ascii=False, indent=2)
        return self.client.generate_json(
            system=SYSTEM,
            prompt=f"USER EDIT INSTRUCTION:\n{instruction.strip()}\n\nCURRENT DRAFT:\n{context}",
            schema=EDIT_SCHEMA,
            thinking_level=os.getenv("GEMINI_EDIT_THINKING_LEVEL", "medium"),
        )

    @staticmethod
    def _valid_section(draft, section):
        return section in {"pleadings","prayer","opening_averment","verification","parties","signature_block","title","court_heading","case_heading"}

    def apply(self, draft: dict[str, Any], edit: dict[str, Any]) -> tuple[dict[str, Any], str]:
        out = deepcopy(draft)
        section = str(edit.get("section", ""))
        operation = str(edit.get("operation", ""))
        old = str(edit.get("old_text", ""))
        new = str(edit.get("new_text", ""))
        index = int(edit.get("index", -1))
        if not self._valid_section(out, section): raise ValueError("Invalid edit section.")
        layout_ops = {"page_break_before","page_break_after","remove_page_break_before","remove_page_break_after"}
        text_ops = {"replace","delete","insert_before","insert_after"}
        if operation not in text_ops | layout_ops: raise ValueError("Unsupported edit operation.")

        if operation in layout_ops:
            if old or new:
                raise ValueError("Layout edits must not contain old_text/new_text.")
            layout = out.setdefault("layout", {})
            if not isinstance(layout, dict):
                layout = {}; out["layout"] = layout
            key = "page_break_before" if "before" in operation else "page_break_after"
            values = layout.get(key, [])
            if not isinstance(values, list): values = [values]
            allowed = {"court_heading","case_heading","parties","title","opening_averment","pleadings","prayer","signature_block","verification"}
            values = [str(x) for x in values if str(x) in allowed]
            if operation.startswith("remove_"):
                values = [x for x in values if x != section]
            elif section not in values:
                values.append(section)
            layout[key] = values
            return out, self.describe(edit)

        scalar = section in {"opening_averment","verification","title","court_heading","case_heading"}
        if scalar:
            current = str(out.get(section, "") or "")
            if index not in (0, -1): raise ValueError("Invalid target index.")
            if not current: raise ValueError("Target section is empty.")
            if operation in {"replace","delete"}:
                if not old or old not in current: raise ValueError("The exact target text was not found; no change applied.")
                replacement = "" if operation == "delete" else new
                out[section] = current.replace(old, replacement, 1)
            else:
                # For scalar fields, insertion is anchored by old_text.
                if not old or old not in current: raise ValueError("The exact anchor text was not found; no change applied.")
                repl = (new + old) if operation == "insert_before" else (old + new)
                out[section] = current.replace(old, repl, 1)
        else:
            items = out.get(section, [])
            if not isinstance(items, list): items = [items]
            if index < 0 or index >= len(items): raise ValueError("Target paragraph/item index is out of range.")
            current = str(items[index])
            if operation in {"replace","delete"}:
                if not old or old not in current: raise ValueError("The exact target text was not found; no change applied.")
                replacement = "" if operation == "delete" else new
                items[index] = current.replace(old, replacement, 1)
            elif operation == "insert_before":
                if old and old not in current: raise ValueError("The exact anchor text was not found; no change applied.")
                items.insert(index, new)
            elif operation == "insert_after":
                if old and old not in current: raise ValueError("The exact anchor text was not found; no change applied.")
                items.insert(index + 1, new)
            out[section] = items
        return out, self.describe(edit)

    @staticmethod
    def describe(edit: dict[str, Any]) -> str:
        op = edit.get("operation")
        section = edit.get("section")
        idx = edit.get("index")
        old = str(edit.get("old_text", ""))
        new = str(edit.get("new_text", ""))
        labels = {"pleadings":"पैराग्राफ","prayer":"प्रार्थना","parties":"पक्षकार","signature_block":"हस्ताक्षर/सिग्नेचर","opening_averment":"प्रारंभिक कथन","verification":"सत्यापन","title":"शीर्षक","court_heading":"न्यायालय शीर्षक","case_heading":"केस शीर्षक"}
        target = labels.get(section, section)
        if section in {"pleadings","prayer","parties","signature_block"}: target += f" {int(idx)+1}"
        if op == "page_break_before": return f"📄 {target} को नए पेज से शुरू किया जाएगा।"
        if op == "page_break_after": return f"📄 {target} के बाद नया पेज शुरू किया जाएगा।"
        if op == "remove_page_break_before": return f"↩️ {target} से पहले का page break हटाया जाएगा।"
        if op == "remove_page_break_after": return f"↩️ {target} के बाद का page break हटाया जाएगा।"
        if op == "delete": return f"🗑️ {target} से यह अंश हटेगा:\n{old}"
        if op == "replace": return f"✏️ {target} में बदलाव:\nBefore: {old}\nAfter: {new}"
        if op == "insert_before": return f"➕ {target} से पहले जोड़ा जाएगा:\n{new}"
        return f"➕ {target} के बाद जोड़ा जाएगा:\n{new}"
