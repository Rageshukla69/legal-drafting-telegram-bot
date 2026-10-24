from __future__ import annotations
import json, logging, os, re
from copy import deepcopy
from typing import Any
from .gemini_client import GeminiClient
from .krutidev_converter import sanitize_draft_unicode
from .legal_number_formatter import format_draft_numbers

log = logging.getLogger("legal-bot.editor")

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
                "batch",
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
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string"},
                    "section": {"type": "string"},
                    "index": {"type": "integer", "minimum": 0},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["operation", "section", "index", "old_text", "new_text", "reason"],
            },
        },
    },
    "required": ["operation", "section", "index", "old_text", "new_text", "reason"],
}

SYSTEM = """You are a precise legal-document edit parser for an Indian advocate's Hindi drafting bot.
Convert the lawyer's natural-language or voice-dictated edit instruction (including requests to add a point, remove a point, remove/edit party names, or update headings/titles) into a structured edit operation against the supplied structured draft.

Rules:
- Never rewrite unrelated parts of the document.
- Select the target `section` and zero-based `index` (use 0 for scalar sections or when using `append` / `replace_all`).
- Voice Dictation (STT) Phonetic Awareness:
  * Instructions often come from Hindi speech-to-text, which may have phonetic spellings such as "साक्षेतु शपथपत्र" for "साक्ष्य हेतु शपथपत्र", "वादीगढ़" for "वादीगण", or "वीरेंद्र" (with anusvara ं) when the draft has "वीरेन्द्र" (with half-ना न्).
  * Always inspect `CURRENT DRAFT` carefully and copy `old_text` CHARACTER-FOR-CHARACTER as it actually appears in `CURRENT DRAFT` (not the noisy STT spelling).
  * Write `new_text` in clean, formal Unicode Devanagari legal Hindi (e.g. if the user says "साक्षेतु शपथपत्र PW-6 कोष्ठक में मुनेश कुमार लिख दो", set `new_text` to "साक्ष्य हेतु शपथपत्र PW-6 (मुनेश कुमार)").
  * Whenever the user says "कोष्ठक में X" or "ब्रैकेट में X", format it with parentheses `(X)` instead of writing the words "कोष्ठक में".
- Supported operations:
  * `replace`: replace `old_text` with `new_text` in the selected section/index. `old_text` MUST be copied verbatim from the selected item in `CURRENT DRAFT`.
  * `replace_all`: when the lawyer wants a name, Gata/Khasra number, date, or term corrected or removed everywhere it appears across the entire document, set `operation="replace_all"`, `old_text` to the exact existing text, and `new_text` to replacement (or `""` if deleting everywhere).
  * `delete`: delete `old_text` from the selected item, OR if the lawyer wants an entire point/paragraph/party line removed, set `old_text` to the ENTIRE text of that item in `pleadings`, `prayer`, or `parties` so the whole item is removed cleanly. When removing one person's name from a multi-party line, include any trailing comma or connector in `old_text` so the remaining party text reads cleanly.
  * `insert_before` / `insert_after`: insert a new point/paragraph `new_text` before or after `index` in list sections (`pleadings`, `prayer`, `parties`, `signature_block`).
  * `append`: add a new point/paragraph/clause `new_text` to the end of `pleadings`, `prayer`, `parties`, or `signature_block` (`old_text` should be empty).
  * `batch`: if the lawyer gave multiple distinct changes in one message (for example, removing a name from both वादीगण and प्रतिवादीगण in `parties`, AND updating `title`), set `operation="batch"` and put each individual change in `edits`.
- Layout operations: `page_break_before`, `page_break_after`, `remove_page_break_before`, `remove_page_break_after`.
- Always write Hindi legal additions/replacements in clean Unicode Devanagari Hindi (starting new pleading paragraphs with "यह कि " where natural, without numbering).
- Return JSON only matching the schema.
"""


def _normalize_hindi_punct(text: str) -> str:
    t = text.replace("०", "0").replace("।", ".")
    t = re.sub(r"[\-–—]", "-", t)
    return t


def _build_phonetic_skeleton(text: str) -> tuple[str, list[int]]:
    """Build a normalized Devanagari skeleton and a char-index map back to `text`.

    Unifies:
    - Anusvara (ं), Chandrabindu (ँ), and half-nasals (न्, म्, ण्, ञ्, ङ्) -> 'N'
    - Nukta (\u093c) -> stripped
    - Multiple spaces, hyphens, and punctuation separators
    - Common STT contractions like 'साक्षेतु' <-> 'साक्ष्यहेतु' and 'वादीगढ़' <-> 'वादीगण'
    """
    skel_chars: list[str] = []
    idx_map: list[int] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        # Skip nukta or zero-width joiners
        if ch in {"\u093c", "\u200c", "\u200d"}:
            i += 1
            continue
        # Half-nasal consonant (न/म/ण/ञ/ङ + हलन्त ्) -> 'N'
        if ch in {"न", "म", "ण", "ञ", "ङ"} and i + 1 < n and text[i + 1] == "\u094d":
            skel_chars.append("N")
            idx_map.append(i)
            i += 2
            continue
        # Anusvara / Chandrabindu -> 'N'
        if ch in {"\u0902", "\u0901"}:
            skel_chars.append("N")
            idx_map.append(i)
            i += 1
            continue
        # Normalize 'ढ़' / 'ढ' -> 'ण' only for plural suffix 'गढ़' -> 'गण'
        if ch == "ढ" and i > 0 and text[i - 1] in {"ग", "ण"}:
            skel_chars.append("ण")
            idx_map.append(i)
            i += 1
            continue
        # Skip hyphens and spaces so 'शपथ-पत्र', 'शपथ पत्र', 'शपथपत्र' unify
        if ch.isspace() or ch in {"-", "–", "—"}:
            i += 1
            continue
        if ch == "०":
            ch = "0"
        elif ch == "।":
            ch = "."
        skel_chars.append(ch.casefold())
        idx_map.append(i)
        i += 1
    return "".join(skel_chars), idx_map


def _cleanup_party_or_sentence_punctuation(text: str) -> str:
    """Clean up dangling commas, double spaces, or empty serial markers left after deleting a substring."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    # Remove orphan serial numbers whose name was deleted, e.g. ", 2. —" or "1. ,"
    cleaned = re.sub(r"(?:,\s*)?\b\d+\s*(?:\([^)]+\))?\s*[.)]\s*(?=[,;:\-–—]|$)", "", cleaned)
    cleaned = re.sub(r",\s*(?=[\-–—])", " ", cleaned)
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"^\s*[,;:\-–—]+\s*", "", cleaned)
    cleaned = re.sub(r"\s*[,;]+\s*$", "", cleaned)
    cleaned = re.sub(r"\b(?:व|तथा|एवं)\s*(?:व|तथा|एवं)\b", "व", cleaned)
    cleaned = re.sub(r",\s*(?:व|तथा|एवं)\s*,", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # If only a bare role label remains (e.g. "— वादीगण" or "वादी"), treat as empty so item is popped
    if re.match(r"^[\-–—.\s]*(?:वादीगण|वादी|प्रतिवादीगण|प्रतिवादी|विपक्षीगण|विपक्षी)[\-–—.\s]*$", cleaned):
        return ""
    return cleaned


def _flex_replace_once(source: str, old: str, new: str) -> str | None:
    """Replace `old` in `source`, with whitespace, punctuation, and Devanagari nasal/phonetic flexibility."""
    if not old:
        return None
    if old in source:
        return source.replace(old, new, 1)

    clean_old = old.strip().rstrip(" .।:,;-–—")
    if clean_old and clean_old in source:
        return source.replace(clean_old, new, 1)

    tokens = [re.escape(tok) for tok in clean_old.split() if tok]
    if not tokens:
        return None
    pattern = re.compile(r"\s+".join(tokens))
    m = pattern.search(source)
    if m:
        return source[:m.start()] + new + source[m.end():]

    norm_source = _normalize_hindi_punct(source)
    norm_old = _normalize_hindi_punct(clean_old)
    if norm_old and norm_old in norm_source:
        start_idx = norm_source.find(norm_old)
        end_idx = start_idx + len(norm_old)
        return source[:start_idx] + new + source[end_idx:]

    # Devanagari phonetic/orthographic skeleton match (e.g. वीरेंद्र <-> वीरेन्द्र, शपथ-पत्र <-> शपथपत्र)
    src_skel, src_map = _build_phonetic_skeleton(source)
    old_skel, _ = _build_phonetic_skeleton(clean_old)
    # Also handle STT contraction 'साक्षेतु' -> 'साक्ष्यहेतु'
    alt_old_skels = [old_skel]
    if "साक्षेतु" in old_skel:
        alt_old_skels.append(old_skel.replace("साक्षेतु", "साक्ष्यहेतु"))
        alt_old_skels.append(old_skel.replace("साक्षेतु", "साक्ष्यका"))
    # Also try without leading honorific 'श्री' if draft omitted 'श्री' or vice versa
    if old_skel.startswith("श्री") and len(old_skel) > 4:
        alt_old_skels.append(old_skel[len("श्री"):])

    for cand_skel in alt_old_skels:
        if not cand_skel:
            continue
        pos = src_skel.find(cand_skel)
        if pos != -1:
            start_orig = src_map[pos]
            last_mapped = src_map[pos + len(cand_skel) - 1]
            end_orig = last_mapped + 1
            # Include any trailing combining marks / halant / matras belonging to the last character
            while end_orig < len(source) and (
                "\u0900" <= source[end_orig] <= "\u0903"
                or "\u093a" <= source[end_orig] <= "\u094f"
                or "\u0951" <= source[end_orig] <= "\u0957"
                or "\u0962" <= source[end_orig] <= "\u0963"
            ):
                end_orig += 1
            return source[:start_orig] + new + source[end_orig:]

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
            "reason": "Unambiguous request to start section on the next page.",
        }

    @staticmethod
    def _deterministic_remove_paragraph(draft: dict[str, Any], instruction: str) -> dict[str, Any] | None:
        """Fast-path for unambiguous paragraph removal like 'पैराग्राफ 3 हटा दो' or 'paragraph 2 delete'."""
        text = re.sub(r"\s+", " ", str(instruction or "").strip().casefold())
        m = re.match(
            r"^(?:पैराग्राफ|पैरा|पॉइंट|बिंदु|दावा पैरा|paragraph|para|point)?\s*(\d+)\s*(?:को\s*)?(?:हटा\s*दो|हटाओ|डिलीट\s*करो|हटाना\s*है|delete|remove)?$",
            text,
        )
        if not m:
            return None
        has_delete_word = any(w in text for w in ("हटा", "डिलीट", "delete", "remove"))
        if not has_delete_word and not text.isdigit():
            return None
        idx = int(m.group(1)) - 1
        pleadings = draft.get("pleadings") or []
        if 0 <= idx < len(pleadings):
            return {
                "operation": "delete",
                "section": "pleadings",
                "index": idx,
                "old_text": str(pleadings[idx]),
                "new_text": "",
                "reason": f"Remove paragraph {idx + 1}",
            }
        return None

    def parse_add_point(self, draft: dict[str, Any], instruction: str) -> dict[str, Any]:
        """Dedicated parser when the user explicitly clicked 'Add Point'."""
        clean = instruction.strip()
        # If the user actually gave a general edit/delete/replace instruction while in add_point mode, route to general parser
        low = clean.casefold()
        if any(k in low for k in ("हटा दो", "हटाओ", "के स्थान पर", "की जगह", "लिख दो", "बदल दो", "कोष्ठक में")):
            return self.parse(draft, clean)

        clean_body = re.sub(
            r"^(?:यह\s*पॉइंट\s*जोड़\s*दो\s*(?:कि)?|पॉइंट\s*जोड़ो\s*:?|इसमें\s*यह\s*जोड़ो\s*(?:कि)?|add\s*point\s*:?)\s*",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
        if any(k in low for k in ("के पहले", "के बाद", "प्रार्थना में", "सत्यापन में", "before", "after", "in prayer")):
            return self.parse(draft, f"Add this point to the draft: {clean}")

        if clean_body and not clean_body.startswith("यह कि") and any("\u0900" <= c <= "\u097f" for c in clean_body):
            formatted = f"यह कि {clean_body}"
        else:
            formatted = clean_body or clean
        if formatted and not formatted.endswith(("।", ".", "!", "?")):
            formatted += "।"

        pleadings = draft.get("pleadings") or []
        insert_idx = len(pleadings)
        for i, p in enumerate(pleadings):
            if any(k in str(p) for k in ("कारण वाद", "क्षेत्राधिकार", "मूल्यांकन", "न्यायालय शुल्क")):
                insert_idx = i
                break

        if insert_idx < len(pleadings):
            return {
                "operation": "insert_before",
                "section": "pleadings",
                "index": insert_idx,
                "old_text": "",
                "new_text": formatted,
                "reason": "Insert new point before formal closing clauses.",
            }
        return {
            "operation": "append",
            "section": "pleadings",
            "index": max(0, len(pleadings) - 1),
            "old_text": "",
            "new_text": formatted,
            "reason": "Append new point to pleadings.",
        }

    def parse_remove_point(self, draft: dict[str, Any], instruction: str) -> dict[str, Any]:
        """Dedicated parser when the user explicitly clicked 'Remove Point'."""
        direct = self._deterministic_remove_paragraph(draft, instruction)
        if direct:
            return direct
        # Pass the instruction directly to parse() so multi-part edits (e.g. delete party + update title) work seamlessly
        return self.parse(draft, instruction)

    def parse(self, draft: dict[str, Any], instruction: str) -> dict[str, Any]:
        direct = self._deterministic_layout_intent(instruction)
        if direct:
            return direct
        direct_rm = self._deterministic_remove_paragraph(draft, instruction)
        if direct_rm:
            return direct_rm
        context = json.dumps({"draft": draft, "editable_sections": self._sections(draft)}, ensure_ascii=False, indent=2)
        res = self._get_client().generate_json(
            system=SYSTEM,
            prompt=f"USER EDIT INSTRUCTION:\n{instruction.strip()}\n\nCURRENT DRAFT:\n{context}",
            schema=EDIT_SCHEMA,
            thinking_level=os.getenv("GEMINI_EDIT_THINKING_LEVEL", "medium"),
        )
        return sanitize_draft_unicode(res)

    @staticmethod
    def _valid_section(section: str) -> bool:
        return section in {
            "pleadings", "prayer", "opening_averment", "verification",
            "parties", "signature_block", "title", "court_heading", "case_heading",
        }

    def _apply_single_op(self, draft: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
        out = deepcopy(draft)
        section = str(edit.get("section", ""))
        operation = str(edit.get("operation", ""))
        old = str(edit.get("old_text", ""))
        new = str(edit.get("new_text", ""))
        index = int(edit.get("index", -1))

        # Auto-convert spoken "कोष्ठक में X" inside new_text if the LLM left it literal
        new = re.sub(r"\s*कोष्ठक\s+में\s+([^\n,।()]+)", r" (\1)", new).strip()
        edit["new_text"] = new

        if not self._valid_section(section):
            raise ValueError(f"Invalid edit section: {section}")

        layout_ops = {"page_break_before", "page_break_after", "remove_page_break_before", "remove_page_break_after"}
        text_ops = {"replace", "replace_all", "delete", "insert_before", "insert_after", "append"}
        if operation not in text_ops | layout_ops:
            raise ValueError(f"Unsupported edit operation: {operation}")

        if operation in layout_ops:
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
            return out

        if operation == "replace_all":
            if not old:
                raise ValueError("replace_all requires old_text.")
            changed = 0
            for sec_key in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
                val = str(out.get(sec_key, "") or "")
                replaced = _flex_replace_once(val, old, new)
                if replaced is not None and replaced != val:
                    # Replace all occurrences in scalar if multiple exist
                    while True:
                        nxt = _flex_replace_once(replaced, old, new)
                        if nxt is None or nxt == replaced:
                            break
                        replaced = nxt
                    out[sec_key] = _cleanup_party_or_sentence_punctuation(replaced) if not new else replaced
                    changed += 1
            for list_key in ("parties", "pleadings", "prayer", "signature_block"):
                items = out.get(list_key, [])
                if isinstance(items, list):
                    new_items = []
                    for it in items:
                        s_it = str(it)
                        replaced = _flex_replace_once(s_it, old, new)
                        if replaced is not None and replaced != s_it:
                            while True:
                                nxt = _flex_replace_once(replaced, old, new)
                                if nxt is None or nxt == replaced:
                                    break
                                replaced = nxt
                            s_it = _cleanup_party_or_sentence_punctuation(replaced) if not new else replaced
                            changed += 1
                        if s_it.strip():
                            new_items.append(s_it)
                    out[list_key] = new_items
            if not changed:
                raise ValueError(f"Target text '{old}' not found across draft; no change applied.")
            return sanitize_draft_unicode(out)

        scalar = section in {"opening_averment", "verification", "title", "court_heading", "case_heading"}
        if scalar:
            current = str(out.get(section, "") or "")
            if not current:
                if operation in {"replace", "append", "insert_after"} and new:
                    out[section] = new
                    return sanitize_draft_unicode(out)
                raise ValueError(f"Target section '{section}' is empty.")
            if operation in {"replace", "delete"}:
                replacement = "" if operation == "delete" else new
                updated = _flex_replace_once(current, old, replacement)
                if updated is None:
                    # Fallback for short scalar headings (`title`, `court_heading`, `case_heading`):
                    # if replacing the title/heading and `new` is provided, replace the scalar value cleanly
                    if operation == "replace" and section in {"title", "court_heading", "case_heading"} and new.strip():
                        updated = new.strip()
                    else:
                        raise ValueError(f"Target text '{old}' not found in {section}.")
                out[section] = _cleanup_party_or_sentence_punctuation(updated) if operation == "delete" else updated
            elif operation == "append":
                out[section] = (current + " " + new).strip()
            else:
                repl = (new + old) if operation == "insert_before" else (old + new)
                updated = _flex_replace_once(current, old, repl)
                out[section] = updated if updated is not None else (current + " " + new).strip()
        else:
            items = out.get(section, [])
            if not isinstance(items, list):
                items = [items]
            if operation == "append":
                if not new.strip():
                    raise ValueError("Cannot append empty text.")
                items.append(new.strip())
                out[section] = items
                return sanitize_draft_unicode(out)

            if index < 0 or index >= len(items):
                index = max(0, min(index, len(items) - 1))

            current = str(items[index]) if items else ""
            if operation in {"replace", "delete"}:
                if operation == "delete" and (not old.strip() or old.strip() == current.strip()):
                    items.pop(index)
                    out[section] = items
                    return sanitize_draft_unicode(out)
                replacement = "" if operation == "delete" else new

                # Special handling for `parties`: if a party name is being deleted/replaced and appears
                # across multiple party entries (e.g. both वादीगण and प्रतिवादीगण), update all matching entries.
                if section == "parties" and old.strip():
                    matched_any = False
                    updated_parties: list[str] = []
                    for i, party_line in enumerate(items):
                        s_line = str(party_line)
                        cand = _flex_replace_once(s_line, old, replacement)
                        if cand is not None and cand != s_line:
                            matched_any = True
                            if operation == "delete":
                                cand = _cleanup_party_or_sentence_punctuation(cand)
                            if cand.strip():
                                updated_parties.append(cand.strip())
                        else:
                            updated_parties.append(s_line)
                    if matched_any:
                        out[section] = updated_parties
                        return sanitize_draft_unicode(out)

                updated = _flex_replace_once(current, old, replacement)
                if updated is None:
                    found_idx = next((i for i, it in enumerate(items) if _flex_replace_once(str(it), old, replacement) is not None), None)
                    if found_idx is None:
                        raise ValueError(f"Target text '{old}' not found in {section}.")
                    index = found_idx
                    edit["index"] = found_idx
                    updated = _flex_replace_once(str(items[found_idx]), old, replacement)

                if operation == "delete":
                    updated = _cleanup_party_or_sentence_punctuation(updated)
                if operation == "delete" and not updated.strip():
                    items.pop(index)
                else:
                    items[index] = updated.strip()
            elif operation == "insert_before":
                items.insert(index, new)
            elif operation == "insert_after":
                items.insert(index + 1, new)
            out[section] = items

        return sanitize_draft_unicode(out)

    def apply(self, draft: dict[str, Any], edit: dict[str, Any]) -> tuple[dict[str, Any], str]:
        operation = str(edit.get("operation", ""))
        if operation == "batch" and isinstance(edit.get("edits"), list):
            current = deepcopy(draft)
            descriptions = []
            errors = []
            for sub_edit in edit["edits"]:
                try:
                    current = self._apply_single_op(current, sub_edit)
                    descriptions.append(self.describe(sub_edit))
                except Exception as exc:
                    log.warning("Batch sub-edit skipped (%s): %s", sub_edit, exc)
                    errors.append(str(exc))
            if not descriptions and errors:
                raise ValueError("; ".join(errors))
            return format_draft_numbers(sanitize_draft_unicode(current)), "\n".join(descriptions)

        updated = self._apply_single_op(draft, edit)
        return format_draft_numbers(sanitize_draft_unicode(updated)), self.describe(edit)

    @staticmethod
    def describe(edit: dict[str, Any]) -> str:
        if edit.get("operation") == "batch" and isinstance(edit.get("edits"), list):
            lines = [f"{i}. {DraftEditor.describe(sub)}" for i, sub in enumerate(edit["edits"], 1)]
            return "\n".join(lines)

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
            return f"🗑️ {target} से यह अंश हटाया जाएगा:\n{old}"
        if op == "replace":
            return f"✏️ {target} में बदलाव:\nBefore: {old}\nAfter: {new}"
        if op == "insert_before":
            return f"➕ {target} से पहले नया बिंदु जोड़ा जाएगा:\n{new}"
        return f"➕ {target} के बाद नया बिंदु जोड़ा जाएगा:\n{new}"
