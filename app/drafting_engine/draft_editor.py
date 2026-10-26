from __future__ import annotations
import json, logging, os, re
from copy import deepcopy
from typing import Any
from .gemini_client import LunaClient
from .krutidev_converter import sanitize_draft_unicode
from .legal_number_formatter import format_draft_numbers
from .renderers.legal_document_renderer import parse_line_alignment, strip_formatting_markers

log = logging.getLogger("legal-bot.editor")

SECTION_LABELS_HI = {
    "court_heading": "न्यायालय शीर्षक",
    "case_heading": "केस शीर्षक",
    "parties": "पक्षकार",
    "title": "शीर्षक",
    "opening_averment": "प्रारंभिक कथन",
    "pleadings": "पैराग्राफ",
    "prayer": "प्रार्थना",
    "verification": "सत्यापन",
    "signature_block": "हस्ताक्षर/सिग्नेचर",
}

ALIGN_LABELS_HI = {
    "left": "बाएँ (Left Side)",
    "center": "बीच में / मध्य में (Center)",
    "right": "दाएँ (Right Side)",
    "justify": "दोनों ओर बराबर (Justify)",
}

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": [
                "replace",
                "replace_all",
                "rewrite_paragraph",
                "delete",
                "insert_before",
                "insert_after",
                "append",
                "page_break_before",
                "page_break_after",
                "remove_page_break_before",
                "remove_page_break_after",
                "bold_text",
                "unbold_text",
                "align_text",
                "move_item",
                "batch",
            ],
        },
        "scope": {
            "type": "string",
            "enum": ["targeted", "global", "multi_target"],
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

SYSTEM = """You are the Senior Judicial Sansodhan (संशोधन), Typography, Alignment & Scope-Aware Precision Legal Revision Engine powered by Microsoft Foundry GPT-6.0-Luna.

Your job is to understand the advocate's natural-language, voice-dictated, or Document-Intelligence-scanned edit instruction (`USER EDIT INSTRUCTION`) and convert it into a structured edit operation against `CURRENT DRAFT` with **100% accurate Scope Differentiation (`targeted` vs `global` vs `multi_target`)** and **Typography / Alignment / Reordering support**.

CRITICAL SCOPE INTELLIGENCE RULES (`scope` & `operation`):
1. **TARGETED / LOCAL SCOPE (`scope="targeted"`, `operation="replace"` or `"rewrite_paragraph"` or `"delete"`)**:
   - Use `scope="targeted"` whenever the advocate wants to change ONLY a specific paragraph, point, heading, party line, prayer clause, verification, or signature line:
     * Examples of Targeted instructions:
       - `"पैराग्राफ 3 में 15.06.2026 के स्थान पर 20.06.2026 कर दो"` -> Change ONLY `pleadings[2]`, do NOT touch `15.06.2026` in other paragraphs!
       - `"सिर्फ शीर्षक (title) में ... बदल दो"` -> Change ONLY `title`, do NOT touch body paragraphs!
       - `"प्रार्थना के बिंदु 2 / (ख) में ..."` -> Change ONLY `prayer[1]`.
       - `"वादी नंबर 1 के नाम में ..."` or `"प्रतिवादी संख्या 2 के पते में ..."` -> Change ONLY that specific item in `parties[index]`.
       - `"सत्यापन में दिनांक / स्थान ... कर दो"` -> Change ONLY `verification`.
       - `"हस्ताक्षर में अधिवक्ता का नाम ... कर दो"` -> Change ONLY `signature_block`.
     * Even if the advocate does not use the word `"सिर्फ"`, if they name a specific section or paragraph number (e.g. `"पैरा 4 में..."`, `"हेडिंग में..."`, `"प्रार्थना में..."`, `"सत्यापन में..."`), you MUST restrict the change to that exact section/index (`scope="targeted"`, `operation="replace"` or `"rewrite_paragraph"`), NEVER `replace_all`!
     * **Sub-Paragraph Precision**: If `old_text` (such as a year `2024` or a name) appears twice inside the SAME paragraph and the advocate wants to change only one specific occurrence in that paragraph (e.g. `"पैरा 4 में बैनामा का सन् 2024 की जगह 2023 कर दो"`), include 2–4 surrounding context words in both `old_text` and `new_text` (e.g. `old_text="बैनामा सन् 2024"`, `new_text="बैनामा सन् 2023"`) so only that exact occurrence inside Paragraph 4 is modified!

2. **GLOBAL / DOCUMENT-WIDE SCOPE (`scope="global"`, `operation="replace_all"`)**:
   - Use `scope="global"` and `operation="replace_all"` ONLY when:
     * The advocate explicitly asks to apply the change everywhere (`"पूरे ड्राफ्ट में..."`, `"सब जगह..."`, `"हर जगह..."`, `"जहाँ-जहाँ ... लिखा है..."`, `"everywhere"`, `"all places"`), OR
     * The advocate states a general case-wide correction WITHOUT naming any specific paragraph or section (e.g., `"गाटा संख्या 412 गलत लिख गया है, उसे 415 कर दो"`, `"वादी के पिता का नाम गंगादीन की जगह रामदीन कर दो"`, `"सन् 2023 की जगह 2024 कर दो"`) where that entity/fact is a global property of the case that should be consistent across headings, pleadings, and verification.

3. **TYPOGRAPHY, ALIGNMENT & PARAGRAPH MOVEMENT OPERATIONS**:
   - **Bold / Unbold (`operation="bold_text"` / `"unbold_text"`)**:
     * When the advocate wants to make any word, phrase, name, or paragraph bold (e.g. `"बनाम को बोल्ड कर दो"`, `"पैरा 3 में गाटा संख्या 412 को बोल्ड कर दो"`, `"मंजू लता आदि बनाम राजरानी को बोल्ड कर दो"`), use `operation="bold_text"`, put the exact phrase to bold in `old_text` (or `""` to bold the entire item at `section[index]`), and set `new_text` to `f"**{old_text}**"`.
     * To remove bolding, use `operation="unbold_text"`.
   - **Horizontal Alignment (`operation="align_text"`)**:
     * When the advocate wants to move a line or text to the left (`लेफ्ट` / `बाएँ`), center (`बीच में` / `सेंटर` / `मध्य में`), right (`राइट` / `दाएँ`), or justify (`जस्टिफाई`):
       - Set `operation="align_text"`.
       - Set `new_text` to `"left"`, `"center"`, `"right"`, or `"justify"`.
       - Put the target phrase/line in `old_text` (e.g. `"दिनांक"` when moving the date line in `signature_block` to the left: `section="signature_block"`, `old_text="दिनांक"`, `new_text="left"`; or `"श्रीमती मंजू लता आदि बनाम राजरानी"` when centering the cause title: `section="parties"`, `old_text="श्रीमती मंजू लता आदि बनाम राजरानी"`, `new_text="center"`).
   - **Move / Reorder Paragraph or Item (`operation="move_item"`)**:
     * When the advocate wants to move a paragraph (`pleadings`), party (`parties`), prayer clause (`prayer`), or signature line (`signature_block`) up (`ऊपर`), down (`नीचे`), to the top (`सबसे ऊपर`), to the bottom (`सबसे नीचे`), to the middle (`बीच में`), or to a specific position (`पैरा 4 को पैरा 2 के ऊपर कर दो`):
       - Set `operation="move_item"`, `section="pleadings"` (or target section), `index` to the 0-based current index of the item being moved, and `new_text` to `"up"`, `"down"`, `"top"`, `"bottom"`, `"middle"`, or `"index:K"` (where `K` is the 0-based destination index).

4. **MULTI-TARGET SCOPE (`scope="multi_target"`, `operation="batch"`)**:
   - Use `scope="multi_target"` and `operation="batch"` when the advocate asks for 2 or more operations at once (e.g., `"श्रीमती मंजू लता आदि बनाम राजरानी को बीच में ले जाओ और बनाम को बोल्ड कर दो"`, or `"दिनांक को लेफ्ट में कर दो और पैरा 3 को ऊपर कर दो"`), placing each operation in `edits`.

5. **SENIOR COUNSEL LEGAL QUALITY (`new_text`)**:
   - When adding a new point (`insert_before`, `insert_after`, `append`) or rewriting/expanding a paragraph (`rewrite_paragraph`), write rich, authoritative, court-ready Unicode Devanagari Hindi starting with `"यह कि ..."` (without leading paragraph numbers), matching the exact party roles (`वादी`, `प्रतिवादीगण`, `आपत्तिकर्त्री`, `शपथकर्त्री`) of `CURRENT DRAFT`.
   - Format factual numbers in bilingual format (`20,000 (बीस हजार)`).
   - In Dava (`वाद-पत्र`) and Written Statement (`जवाब दावा`), NEVER insert statutory sections (`धारा`), CPC, or BNSS into `pleadings`.

6. **VOICE DICTATION (STT) & DOCUMENT INTELLIGENCE OCR AWARENESS**:
   - Copy `old_text` CHARACTER-FOR-CHARACTER as it appears in `CURRENT DRAFT` (even if the user's voice transcript or OCR had phonetic spelling like `"वीरेंद्र"` vs `"वीरेन्द्र"` or `"साक्षेतु"` vs `"साक्ष्य हेतु"`).
   - Format `"कोष्ठक में X"` or `"ब्रैकेट में X"` as `(X)`.
   - Return JSON only matching the schema.
"""

ADD_POINT_LUNA_SYSTEM = """You are the Senior Judicial Paragraph Composer powered by Microsoft Foundry GPT-6.0-Luna.

The advocate clicked '➕ बिंदु जोड़ें (Add Point)' and provided raw facts, voice dictation, or Document Intelligence text to add to `CURRENT DRAFT`.

Your task:
1. Transform the advocate's raw input into a rich, formal, court-ready Hindi legal paragraph starting with `"यह कि ..."` (without leading paragraph numbers) that seamlessly matches the tone, party terminology (`वादी`, `प्रतिवादीगण`, `आपत्तिकर्त्री`, `शपथग्रहीता`), and context of `CURRENT DRAFT`.
2. Format every numerical value in bilingual court format (digits + Hindi words in parentheses, e.g., `10,000 (दस हजार) रुपये`). Preserve all specific numbers, dates, names, and Gata/Khasra details from the advocate's instruction.
3. If `CURRENT DRAFT` is a Dava (`वाद-पत्र`) or Written Statement (`जवाब दावा`), do NOT add any legal sections (`धारा`), CPC, or BNSS citations.
4. If the advocate specified a particular position (e.g. `"पैरा 3 के बाद जोड़ो"`, `"प्रार्थना में जोड़ो"`), insert it at that exact section and index (`scope="targeted"`). Otherwise, `insert_before` the formal closing clauses (`कारण वाद` / `क्षेत्राधिकार` / `मूल्यांकन`) or `append` to `pleadings`.
5. If the advocate provided multiple distinct points, use `operation="batch"` and add each point as its own formal paragraph.
6. Return JSON only matching the schema.
"""

DOCUMENT_SANSODHAN_LUNA_SYSTEM = """You are the Azure Document Intelligence + Microsoft Foundry GPT-6.0-Luna Legal Revision Engine.

The advocate has uploaded a document/image (e.g., a handwritten correction note, a marked-up printed page, an order sheet, or a reference PDF/DOCX) while editing `CURRENT DRAFT`.
Azure Document Intelligence has extracted `DOCUMENT_INTELLIGENCE_EXTRACTED_TEXT` from their upload, along with optional `USER_CAPTION_INSTRUCTION`.

Your task:
1. Analyze `DOCUMENT_INTELLIGENCE_EXTRACTED_TEXT` and `USER_CAPTION_INSTRUCTION` against `CURRENT DRAFT`.
2. Respect the advocate's requested scope:
   - If `USER_CAPTION_INSTRUCTION` targets a specific paragraph, heading, or section (e.g. `"सिर्फ पैरा 4 को इस कागज के अनुसार बदल दो"`), set `scope="targeted"` and update ONLY that section/paragraph.
   - If the uploaded document contains general corrections (names, parentage, Gata/Khasra numbers, areas, dates, case numbers) or multiple marked edits across the draft, use `replace_all` (`scope="global"`) or `batch` (`scope="multi_target"`).
3. Never drop unrelated paragraphs from `CURRENT DRAFT` unless explicitly instructed to remove them.
4. Return JSON only matching the schema.
"""


def _normalize_hindi_punct(text: str) -> str:
    t = str(text or "").translate(str.maketrans("०१२३४५६७८९", "0123456789")).replace("।", ".")
    t = re.sub(r"[\-–—]", "-", t)
    return t


def _build_phonetic_skeleton(text: str) -> tuple[str, list[int]]:
    """Build a normalized Devanagari skeleton and a char-index map back to `text`."""
    skel_chars: list[str] = []
    idx_map: list[int] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in {"\u093c", "\u200c", "\u200d", "*"}:
            i += 1
            continue
        if ch in {"न", "म", "ण", "ञ", "ङ"} and i + 1 < n and text[i + 1] == "\u094d":
            skel_chars.append("N")
            idx_map.append(i)
            i += 2
            continue
        if ch in {"\u0902", "\u0901"}:
            skel_chars.append("N")
            idx_map.append(i)
            i += 1
            continue
        if ch == "ढ" and i > 0 and text[i - 1] in {"ग", "ण"}:
            skel_chars.append("ण")
            idx_map.append(i)
            i += 1
            continue
        if ch.isspace() or ch in {"-", "–", "—"}:
            i += 1
            continue
        if "०" <= ch <= "९":
            ch = str(ord(ch) - ord("०"))
        elif ch == "।":
            ch = "."
        skel_chars.append(ch.casefold())
        idx_map.append(i)
        i += 1
    return "".join(skel_chars), idx_map


def _cleanup_party_or_sentence_punctuation(text: str) -> str:
    """Clean up dangling commas, double spaces, or empty serial markers left after deleting a substring."""
    cleaned, align_ov = parse_line_alignment(str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"(?:,\s*)?\b\d+\s*(?:\([^)]+\))?\s*[.)]\s*(?=[,;:\-–—]|$)", "", cleaned)
    cleaned = re.sub(r",\s*(?=[\-–—])", " ", cleaned)
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"^\s*[,;:\-–—]+\s*", "", cleaned)
    cleaned = re.sub(r"\s*[,;]+\s*$", "", cleaned)
    cleaned = re.sub(r"\b(?:व|तथा|एवं)\s*(?:व|तथा|एवं)\b", "व", cleaned)
    cleaned = re.sub(r",\s*(?:व|तथा|एवं)\s*,", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if re.match(r"^[\-–—.\s*]*(?:वादीगण|वादी|प्रतिवादीगण|प्रतिवादी|विपक्षीगण|विपक्षी|प्रार्थीगण|प्रार्थी|आपत्तिकर्त्री)[\-–—.\s*]*$", cleaned):
        return ""
    if align_ov and cleaned:
        return f"<<ALIGN:{align_ov.upper()}>>{cleaned}"
    return cleaned


def _flex_replace_once(source: str, old: str, new: str) -> str | None:
    """Replace `old` in `source`, with whitespace, punctuation, and Devanagari nasal/phonetic flexibility.
    Also cleans up any stale Hindi number word in parentheses immediately following a replaced number."""
    if not old:
        return None

    def _post_clean_stale_num_words(prefix: str, suffix: str, old_s: str, new_s: str) -> str:
        if re.search(r"[0-9०-९]\s*$", old_s.strip()) and re.search(r"[0-9०-९]\s*$", new_s.strip()) and "(" not in new_s:
            suffix = re.sub(r"^\s*\([\u0900-\u097F\s]+\)", "", suffix)
        return prefix + new_s + suffix

    if old in source:
        idx = source.find(old)
        return _post_clean_stale_num_words(source[:idx], source[idx + len(old):], old, new)

    clean_old = old.strip().rstrip(" .।:,;-–—")
    if clean_old and clean_old in source:
        idx = source.find(clean_old)
        return _post_clean_stale_num_words(source[:idx], source[idx + len(clean_old):], clean_old, new)

    tokens = [re.escape(tok) for tok in clean_old.split() if tok]
    if not tokens:
        return None
    pattern = re.compile(r"\s+".join(tokens))
    m = pattern.search(source)
    if m:
        return _post_clean_stale_num_words(source[:m.start()], source[m.end():], clean_old, new)

    norm_source = _normalize_hindi_punct(source)
    norm_old = _normalize_hindi_punct(clean_old)
    if norm_old and norm_old in norm_source:
        start_idx = norm_source.find(norm_old)
        end_idx = start_idx + len(norm_old)
        return _post_clean_stale_num_words(source[:start_idx], source[end_idx:], clean_old, new)

    src_skel, src_map = _build_phonetic_skeleton(source)
    old_skel, _ = _build_phonetic_skeleton(clean_old)
    alt_old_skels = [old_skel]
    if "साक्षेतु" in old_skel:
        alt_old_skels.append(old_skel.replace("साक्षेतु", "साक्ष्यहेतु"))
        alt_old_skels.append(old_skel.replace("साक्षेतु", "साक्ष्यका"))
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
            while end_orig < len(source) and (
                "\u0900" <= source[end_orig] <= "\u0903"
                or "\u093a" <= source[end_orig] <= "\u094f"
                or "\u0951" <= source[end_orig] <= "\u0957"
                or "\u0962" <= source[end_orig] <= "\u0963"
            ):
                end_orig += 1
            return _post_clean_stale_num_words(source[:start_orig], source[end_orig:], clean_old, new)

    return None


def _set_line_alignment_marker(text: str, align_dir: str) -> str:
    """Set or replace the leading `<<ALIGN:...>>` tag on `text`."""
    clean_body, _ = parse_line_alignment(str(text or ""))
    direction = str(align_dir or "center").strip().upper()
    if direction not in {"LEFT", "CENTER", "RIGHT", "JUSTIFY"}:
        direction = "CENTER"
    return f"<<ALIGN:{direction}>>{clean_body}"


def _wrap_bold_substring(source: str, target_sub: str) -> str | None:
    """Wrap `target_sub` inside `source` with `**...**` without creating `****`."""
    if not target_sub.strip():
        clean_body, align_ov = parse_line_alignment(source)
        inner = clean_body.replace("**", "").strip()
        if not inner:
            return None
        res = f"**{inner}**"
        return f"<<ALIGN:{align_ov.upper()}>>{res}" if align_ov else res

    clean_target = target_sub.replace("**", "").strip()
    replaced = _flex_replace_once(source, clean_target, f"**{clean_target}**")
    if replaced is None:
        return None
    replaced = re.sub(r"\*{3,}", "**", replaced)
    return replaced


def _unwrap_bold_substring(source: str, target_sub: str) -> str | None:
    """Remove `**...**` around `target_sub` (or from the entire `source` if `target_sub` is empty)."""
    if not target_sub.strip():
        if "**" not in source:
            return source
        return source.replace("**", "")
    clean_target = target_sub.replace("**", "").strip()
    if f"**{clean_target}**" in source:
        return source.replace(f"**{clean_target}**", clean_target)
    replaced = _flex_replace_once(source, f"**{clean_target}**", clean_target)
    if replaced is not None:
        return replaced
    if _flex_replace_once(source, clean_target, "__CHK__") is not None:
        return source.replace("**", "")
    return None


def count_occurrences_across_draft(draft: dict[str, Any], old_text: str) -> list[tuple[str, int, str]]:
    """Return a list of (section, index, human_label) for every section/item in `draft` where `old_text` matches."""
    old_clean = str(old_text or "").strip()
    if not old_clean:
        return []
    matches: list[tuple[str, int, str]] = []
    for sec in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
        val = str(draft.get(sec, "") or "")
        if val and _flex_replace_once(val, old_clean, "__CHK__") is not None:
            matches.append((sec, 0, SECTION_LABELS_HI.get(sec, sec)))
    for list_sec in ("parties", "pleadings", "prayer", "signature_block"):
        items = draft.get(list_sec) or []
        if isinstance(items, list):
            for idx, it in enumerate(items):
                s_it = str(it or "")
                if s_it and _flex_replace_once(s_it, old_clean, "__CHK__") is not None:
                    matches.append((list_sec, idx, f"{SECTION_LABELS_HI.get(list_sec, list_sec)} {idx + 1}"))
    return matches


def convert_edit_scope(draft: dict[str, Any], edit: dict[str, Any], target_scope: str) -> dict[str, Any]:
    """Convert an edit between `targeted` (single-section/paragraph) and `global` (`replace_all` across the entire draft)."""
    updated_edit = deepcopy(edit)
    op = str(updated_edit.get("operation") or "")
    if op in {"bold_text", "unbold_text"}:
        updated_edit["scope"] = "global" if target_scope == "global" else "targeted"
        return updated_edit

    old_t = str(updated_edit.get("old_text") or "").strip()
    new_t = str(updated_edit.get("new_text") or "").strip()
    if not old_t and updated_edit.get("operation") == "batch" and updated_edit.get("edits"):
        first_sub = updated_edit["edits"][0]
        old_t = str(first_sub.get("old_text") or "").strip()
        new_t = str(first_sub.get("new_text") or "").strip()
        updated_edit["old_text"] = old_t
        updated_edit["new_text"] = new_t
        updated_edit["section"] = first_sub.get("section", "pleadings")
        updated_edit["index"] = int(first_sub.get("index", 0))

    if target_scope == "global":
        updated_edit["_orig_section"] = updated_edit.get("section", "pleadings")
        updated_edit["_orig_index"] = int(updated_edit.get("index", 0))
        updated_edit["operation"] = "replace_all"
        updated_edit["scope"] = "global"
        return updated_edit

    orig_sec = updated_edit.get("_orig_section") or updated_edit.get("section") or "pleadings"
    orig_idx = int(updated_edit.get("_orig_index", updated_edit.get("index", 0)) or 0)
    matches = count_occurrences_across_draft(draft, old_t)
    if matches and not any(s == orig_sec and i == orig_idx for s, i, _ in matches):
        orig_sec, orig_idx, _ = matches[0]
    updated_edit["operation"] = "replace"
    updated_edit["scope"] = "targeted"
    updated_edit["section"] = orig_sec
    updated_edit["index"] = orig_idx
    return updated_edit


def _sync_verification_paragraph_count(draft: dict[str, Any], old_count: int) -> dict[str, Any]:
    """Automatically update paragraph count references in `verification` when paragraphs are added or removed."""
    ver = str(draft.get("verification") or "").strip()
    if not ver:
        return draft
    pleadings = [
        strip_formatting_markers(str(p))
        for p in (draft.get("pleadings") or [])
        if strip_formatting_markers(str(p)) and not strip_formatting_markers(str(p)).startswith("अतिरिक्त कथन")
    ]
    new_count = len(pleadings)
    if new_count <= 0 or new_count == old_count:
        return draft

    updated_ver = re.sub(
        rf"((?:धारा|पैरा|प्रस्तर|कंडिका)\s*\d+(?:\s*\([^)]+\))?\s*(?:से|लगायत)\s*){old_count}(?:\s*\([^)]+\))?",
        rf"\g<1>{new_count}",
        ver,
    )
    if updated_ver != ver:
        draft["verification"] = updated_ver
    return draft


def _format_align_badge(text: str) -> str:
    _, align_ov = parse_line_alignment(text)
    if not align_ov:
        return ""
    badges = {"left": " `[⬅️ बाएँ]`", "center": " `[↔️ बीच में]`", "right": " `[➡️ दाएँ]`", "justify": " `[⇔ बराबर]`"}
    return badges.get(align_ov, "")


def format_draft_section_map(draft: dict[str, Any]) -> str:
    """Build a scannable numbered map of all sections and paragraphs in `draft` so the user can target any area."""
    lines: list[str] = []
    if draft.get("court_heading"):
        raw_c = str(draft["court_heading"]).strip()
        lines.append(f"🏛️ *न्यायालय शीर्षक (`court_heading`):*{_format_align_badge(raw_c)} {strip_formatting_markers(raw_c, keep_bold=True)[:110]}")
    if draft.get("case_heading"):
        raw_cs = str(draft["case_heading"]).strip()
        lines.append(f"📁 *केस शीर्षक (`case_heading`):*{_format_align_badge(raw_cs)} {strip_formatting_markers(raw_cs, keep_bold=True)[:110]}")
    parties = [str(x).strip() for x in (draft.get("parties") or []) if str(x).strip()]
    if parties:
        lines.append("👥 *पक्षकार (`parties`):*")
        for i, p in enumerate(parties[:8], 1):
            lines.append(f"  • `पक्षकार {i}:`{_format_align_badge(p)} {strip_formatting_markers(p, keep_bold=True)[:95]}")
    if draft.get("title"):
        raw_t = str(draft["title"]).strip()
        t_flat = strip_formatting_markers(raw_t, keep_bold=True).replace("\n", " / ")
        lines.append(f"📌 *शीर्षक (`title`):*{_format_align_badge(raw_t)} {t_flat[:130]}")
    if draft.get("opening_averment"):
        raw_op = str(draft["opening_averment"]).strip()
        lines.append(f"🗣️ *प्रारंभिक कथन (`opening_averment`):*{_format_align_badge(raw_op)} {strip_formatting_markers(raw_op, keep_bold=True)[:100]}")

    pleadings = [str(x).strip() for x in (draft.get("pleadings") or []) if str(x).strip()]
    if pleadings:
        start_num = int((draft.get("layout") or {}).get("start_paragraph_number", 1) or 1)
        lines.append(f"📋 *पैराग्राफ सूची (`pleadings` — कुल {len(pleadings)}):*")
        p_no = start_num - 1
        for idx, p in enumerate(pleadings[:20], 1):
            p_clean = strip_formatting_markers(p, keep_bold=True)
            if p_clean.lstrip("* ").startswith("अतिरिक्त कथन"):
                lines.append(f"  🔹 *[उप-शीर्षक — इंडेक्स {idx}]:*{_format_align_badge(p)} `{p_clean}`")
            else:
                p_no += 1
                short = (p_clean[:82].rstrip() + "…") if len(p_clean) > 82 else p_clean
                lines.append(f"  • `पैरा {p_no}` _(सूची क्रम {idx})_{_format_align_badge(p)}: {short}")
        if len(pleadings) > 20:
            lines.append(f"  • _… (+ {len(pleadings) - 20} अन्य पैराग्राफ)_")

    prayer = [str(x).strip() for x in (draft.get("prayer") or []) if str(x).strip()]
    if prayer:
        lines.append(f"⚖️ *प्रार्थना (`prayer` — कुल {len(prayer)} बिंदु):*")
        for i, pr in enumerate(prayer[:6], 1):
            pr_clean = strip_formatting_markers(pr, keep_bold=True)
            short_pr = (pr_clean[:90].rstrip() + "…") if len(pr_clean) > 90 else pr_clean
            lines.append(f"  • `प्रार्थना {i}:`{_format_align_badge(pr)} {short_pr}")

    if draft.get("verification"):
        raw_v = str(draft["verification"]).strip()
        v_short = strip_formatting_markers(raw_v, keep_bold=True).replace("\n", " ")
        lines.append(f"✅ *सत्यापन (`verification`):*{_format_align_badge(raw_v)} {v_short[:115]}…")
    sig = [str(x).strip() for x in (draft.get("signature_block") or []) if str(x).strip()]
    if sig:
        sig_disp = [f"{strip_formatting_markers(s, keep_bold=True)}{_format_align_badge(s)}" for s in sig[:4]]
        lines.append(f"✍️ *हस्ताक्षर (`signature_block`):* {' | '.join(sig_disp)[:140]}")
    return "\n".join(lines)


def audit_draft_consistency(draft: dict[str, Any], document_type: str = "dava_plaint") -> list[str]:
    """Run a comprehensive Pre-Filing Legal & Factual Audit on `draft` to detect unfilled blanks (`....`),
    verification paragraph range mismatches, or prohibited statutory citations in pleadings."""
    issues: list[str] = []
    blanks: list[str] = []

    blank_re = re.compile(r"(?:\.{3,}|…+|__{2,}|\[\s*[^\]]*\s*\])")
    for sec in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
        val = strip_formatting_markers(str(draft.get(sec, "") or ""))
        if val and blank_re.search(val):
            blanks.append(f"• *{SECTION_LABELS_HI.get(sec, sec)}:* `{val[:110]}`")

    for list_sec in ("parties", "pleadings", "prayer", "signature_block"):
        items = draft.get(list_sec) or []
        if isinstance(items, list):
            for idx, it in enumerate(items, 1):
                s_it = strip_formatting_markers(str(it or ""))
                if not s_it:
                    continue
                if list_sec == "parties" and re.match(r"^\.{4,}\s*(?:वादी|प्रतिवादी|प्रार्थी|विपक्षी)", s_it):
                    continue
                if blank_re.search(s_it):
                    blanks.append(f"• *{SECTION_LABELS_HI.get(list_sec, list_sec)} {idx}:* `{s_it[:110]}`")

    pleadings = [
        strip_formatting_markers(str(p))
        for p in (draft.get("pleadings") or [])
        if strip_formatting_markers(str(p)) and not strip_formatting_markers(str(p)).startswith("अतिरिक्त कथन")
    ]
    total_p = len(pleadings)
    ver = strip_formatting_markers(str(draft.get("verification") or ""))
    if ver and total_p > 0 and not (draft.get("layout") or {}).get("single_page_mode"):
        m_range = re.findall(r"(?:से|लगायत)\s*([0-9०-९]+)", ver)
        if m_range:
            last_ver_num = int(m_range[-1].translate(str.maketrans("०१२३४५६७८९", "0123456789")))
            if last_ver_num != total_p:
                issues.append(
                    f"⚠️ *सत्यापन पैराग्राफ संख्या अंतर:* ड्राफ्ट में कुल `{total_p}` पैराग्राफ हैं, किंतु सत्यापन में `{last_ver_num}` अंकित है।"
                )

    if document_type in {"dava_plaint", "written_statement"} and not (draft.get("layout") or {}).get("single_page_mode"):
        for idx, p in enumerate(pleadings, 1):
            p_check = re.sub(r"(?:वाद\s*पत्र|वादपत्र|प्रतिवाद\s*पत्र|शपथ\s*पत्र)\s*(?:की|के)\s*धारा", "", p)
            if any(k in p_check.lower() for k in ("सिविल प्रक्रिया संहिता", "भारतीय नागरिक सुरक्षा संहिता", "bnss", "cpc", "आदेश 7 नियम", "आदेश 39 नियम")):
                issues.append(
                    f"⚠️ *Order VI Rule 2 CPC जांच (पैरा {idx}):* अभिवचन (Pleading) में कानूनी धारा/संहिता का उल्लेख पाया गया।"
                )

    findings: list[str] = []
    if blanks:
        findings.append("📝 *रिक्त स्थान (`....`) जिन्हें भरना शेष है:*\n" + "\n".join(blanks[:10]))
    if issues:
        findings.extend(issues)
    if not findings:
        findings.append(
            f"✅ *ड्राफ्ट पूर्णतः स्वच्छ एवं त्रुटिरहित है!* (कुल पैराग्राफ: `{total_p}`) — कोई रिक्त स्थान या विधिक विसंगति नहीं पाई गई।"
        )
    return findings


_SEC_WORD_MAP = {
    "न्यायालय शीर्षक": "court_heading",
    "कोर्ट हेडिंग": "court_heading",
    "केस शीर्षक": "case_heading",
    "वाद संख्या": "case_heading",
    "शीर्षक": "title",
    "टाइटल": "title",
    "हेडिंग": "title",
    "प्रारंभिक कथन": "opening_averment",
    "प्रार्थना": "prayer",
    "सत्यापन": "verification",
    "तस्दीक": "verification",
    "हस्ताक्षर": "signature_block",
    "सिग्नेचर": "signature_block",
    "पक्षकार": "parties",
    "वादी": "parties",
    "प्रतिवादी": "parties",
}


class DraftEditor:
    def __init__(self):
        self.client: LunaClient | None = None

    def _get_client(self) -> LunaClient:
        if self.client is None:
            self.client = LunaClient()
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
            "scope": "targeted",
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
        text_ascii = text.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
        m = re.match(
            r"^(?:सिर्फ\s*|केवल\s*)?(?:पैराग्राफ|पैरा|पॉइंट|बिंदु|दावा पैरा|धारा|कंडिका|प्रस्तर|paragraph|para|point)?\s*(\d+)\s*(?:को\s*|वाला\s*बिंदु\s*|वाला\s*पैराग्राफ\s*)?(?:हटा\s*दो|हटाओ|डिलीट\s*करो|हटाना\s*है|delete|remove)?$",
            text_ascii,
        )
        if not m:
            return None
        has_delete_word = any(w in text_ascii for w in ("हटा", "डिलीट", "delete", "remove"))
        if not has_delete_word and not text_ascii.isdigit():
            return None
        req_num = int(m.group(1))
        start_num = int((draft.get("layout") or {}).get("start_paragraph_number", 1) or 1)
        idx = (req_num - start_num) if req_num >= start_num else (req_num - 1)
        pleadings = draft.get("pleadings") or []
        if 0 <= idx < len(pleadings):
            return {
                "operation": "delete",
                "scope": "targeted",
                "section": "pleadings",
                "index": idx,
                "old_text": str(pleadings[idx]),
                "new_text": "",
                "reason": f"Remove paragraph {req_num}",
            }
        return None

    @staticmethod
    def _deterministic_scoped_replace(draft: dict[str, Any], instruction: str) -> dict[str, Any] | None:
        """Deterministic fast-path for explicit scoped replacements."""
        raw = re.sub(r"\s+", " ", str(instruction or "").strip())
        if not raw:
            return None
        raw_norm = raw.translate(str.maketrans("०१२३४५६७८९", "0123456789"))

        m_global = re.match(
            r"^(?:पूरे\s*(?:ड्राफ्ट|दस्तावेज़|दस्तावेज|कागज)\s*में|सब\s*जगह|हर\s*जगह|सभी\s*स्थानों\s*पर|everywhere)\s+"
            r"['\"“”]?(.+?)['\"“”]?\s*(?:के\s*स्थान\s*पर|की\s*जगह|को\s*बदलकर|to|with)\s*['\"“”]?(.+?)['\"“”]?\s*"
            r"(?:कर\s*दो|लिख\s*दो|बदल\s*दो|करें|करो)?[।.]?$",
            raw_norm,
            flags=re.I,
        )
        if m_global:
            old_t = m_global.group(1).strip()
            new_t = m_global.group(2).strip()
            if old_t and new_t and count_occurrences_across_draft(draft, old_t):
                return {
                    "operation": "replace_all",
                    "scope": "global",
                    "section": "pleadings",
                    "index": 0,
                    "old_text": old_t,
                    "new_text": new_t,
                    "reason": "Explicit global replacement across entire document.",
                }

        m_para = re.match(
            r"^(?:सिर्फ\s*|केवल\s*|only\s*in\s*)?(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|धारा|कंडिका|प्रस्तर|para|paragraph|point)\s*(?:नं०|नंबर|संख्या)?\s*(\d+)\s*में\s+"
            r"['\"“”]?(.+?)['\"“”]?\s*(?:के\s*स्थान\s*पर|की\s*जगह|को\s*बदलकर)\s*['\"“”]?(.+?)['\"“”]?\s*"
            r"(?:कर\s*दो|लिख\s*दो|बदल\s*दो|करें|करो)?[।.]?$",
            raw_norm,
            flags=re.I,
        )
        if m_para:
            req_num = int(m_para.group(1))
            old_t = m_para.group(2).strip()
            new_t = m_para.group(3).strip()
            start_num = int((draft.get("layout") or {}).get("start_paragraph_number", 1) or 1)
            idx = (req_num - start_num) if req_num >= start_num else (req_num - 1)
            pleadings = draft.get("pleadings") or []
            if 0 <= idx < len(pleadings) and old_t:
                return {
                    "operation": "replace",
                    "scope": "targeted",
                    "section": "pleadings",
                    "index": idx,
                    "old_text": old_t,
                    "new_text": new_t,
                    "reason": f"Targeted replacement strictly in paragraph {req_num}.",
                }

        m_sec = re.match(
            r"^(?:सिर्फ\s*|केवल\s*)?(न्यायालय\s*शीर्षक|कोर्ट\s*हेडिंग|केस\s*शीर्षक|वाद\s*संख्या|शीर्षक|टाइटल|हेडिंग|प्रारंभिक\s*कथन|प्रार्थना|सत्यापन|तस्दीक|हस्ताक्षर|सिग्नेचर|पक्षकार|वादी|प्रतिवादी)\s*(?:(?:नं०|नंबर|संख्या)?\s*(\d+)\s*)?में\s+"
            r"['\"“”]?(.+?)['\"“”]?\s*(?:के\s*स्थान\s*पर|की\s*जगह|को\s*बदलकर)\s*['\"“”]?(.+?)['\"“”]?\s*"
            r"(?:कर\s*दो|लिख\s*दो|बदल\s*दो|करें|करो)?[।.]?$",
            raw_norm,
            flags=re.I,
        )
        if m_sec:
            sec_word = m_sec.group(1).strip()
            sec_idx = max(0, int(m_sec.group(2)) - 1) if m_sec.group(2) else (1 if sec_word == "प्रतिवादी" else 0)
            old_t = m_sec.group(3).strip()
            new_t = m_sec.group(4).strip()
            target_sec = _SEC_WORD_MAP.get(sec_word, "title")
            return {
                "operation": "replace",
                "scope": "targeted",
                "section": target_sec,
                "index": sec_idx,
                "old_text": old_t,
                "new_text": new_t,
                "reason": f"Targeted replacement strictly in {sec_word}.",
            }

        return None

    @staticmethod
    def _deterministic_single_format_or_move(draft: dict[str, Any], instruction: str) -> dict[str, Any] | None:
        """Deterministic fast-path for a single Bold/Unbold, Align (Left/Center/Right), or Paragraph Move instruction."""
        raw = re.sub(r"\s+", " ", str(instruction or "").strip()).strip("।.")
        if not raw:
            return None
        raw_norm = raw.translate(str.maketrans("०१२३४५६७८९", "0123456789"))
        low = raw_norm.casefold()
        start_num = int((draft.get("layout") or {}).get("start_paragraph_number", 1) or 1)

        def _para_to_idx(req_num: int) -> int:
            return (req_num - start_num) if req_num >= start_num else (req_num - 1)

        # ------------------------------------------------------------------
        # 1. PARAGRAPH / ITEM MOVE (ऊपर / नीचे / पहले / बाद में / सबसे ऊपर / सबसे नीचे)
        # ------------------------------------------------------------------
        m_move_rel = re.match(
            r"^(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|धारा|कंडिका|प्रस्तर|para|paragraph|point)\s*(?:नं०|नंबर|संख्या)?\s*(\d+)\s*को\s*"
            r"(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|धारा|कंडिका|प्रस्तर|para|paragraph|point)?\s*(?:नं०|नंबर|संख्या)?\s*(\d+)\s*(?:के\s*|से\s*)?"
            r"(ऊपर|पहले|नीचे|बाद(?:\s*में)?|जगह(?:\s*पर)?|स्थान\s*पर)\s*(?:कर\s*दो|ले\s*जाओ|लाओ|ले\s*आओ|रख\s*दो|स्थानांतरित\s*करो|करो|करें|move)?$",
            raw_norm,
            flags=re.I,
        )
        if m_move_rel:
            src_num = int(m_move_rel.group(1))
            dst_num = int(m_move_rel.group(2))
            rel_word = m_move_rel.group(3).strip()
            src_idx = _para_to_idx(src_num)
            dst_idx = _para_to_idx(dst_num)
            pleadings = draft.get("pleadings") or []
            if 0 <= src_idx < len(pleadings):
                if any(w in rel_word for w in ("ऊपर", "पहले", "जगह", "स्थान")):
                    target_idx = dst_idx if src_idx > dst_idx else max(0, dst_idx - 1)
                else:
                    target_idx = dst_idx if src_idx < dst_idx else min(len(pleadings) - 1, dst_idx + 1)
                return {
                    "operation": "move_item",
                    "scope": "targeted",
                    "section": "pleadings",
                    "index": src_idx,
                    "old_text": strip_formatting_markers(str(pleadings[src_idx]))[:100],
                    "new_text": f"index:{target_idx}",
                    "reason": f"Move paragraph {src_num} relative to paragraph {dst_num}.",
                }

        m_move_between = re.match(
            r"^(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|para|paragraph)\s*(\d+)\s*को\s*(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|para|paragraph)?\s*(\d+)\s*(?:और|व|तथा|एवं)\s*(\d+)\s*के\s*बीच(?:\s*में)?\s*(?:कर\s*दो|ले\s*जाओ|लाओ|ले\s*आओ|रख\s*दो|करो)?$",
            raw_norm,
            flags=re.I,
        )
        if m_move_between:
            src_num = int(m_move_between.group(1))
            y_num = int(m_move_between.group(2))
            src_idx = _para_to_idx(src_num)
            y_idx = _para_to_idx(y_num)
            pleadings = draft.get("pleadings") or []
            if 0 <= src_idx < len(pleadings):
                target_idx = y_idx if src_idx < y_idx else y_idx + 1
                return {
                    "operation": "move_item",
                    "scope": "targeted",
                    "section": "pleadings",
                    "index": src_idx,
                    "old_text": strip_formatting_markers(str(pleadings[src_idx]))[:100],
                    "new_text": f"index:{target_idx}",
                    "reason": f"Move paragraph {src_num} between paragraphs.",
                }

        m_move_dir = re.match(
            r"^(?:(प्रार्थना|पक्षकार|हस्ताक्षर)\s*(?:के\s*)?)?(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|धारा|कंडिका|प्रस्तर|para|paragraph|point)?\s*(?:नं०|नंबर|संख्या)?\s*(\d+)\s*को\s*"
            r"(?:(\d+)\s*(?:पैरा|स्टेप|कदम|नंबर|पायदान)?\s*)?(सबसे\s*ऊपर|सबसे\s*नीचे|अंत\s*में|आखिर\s*में|शुरू\s*में|प्रारंभ\s*में|ऊपर|नीचे|up|down|top|bottom)\s*"
            r"(?:कर\s*दो|ले\s*जाओ|लाओ|ले\s*आओ|रख\s*दो|खिसका\s*दो|शिफ्ट\s*कर\s*दो|स्थानांतरित\s*करो|करो|करें|move)?$",
            raw_norm,
            flags=re.I,
        )
        if m_move_dir:
            sec_prefix = (m_move_dir.group(1) or "").strip()
            req_num = int(m_move_dir.group(2))
            step_str = m_move_dir.group(3)
            dir_word = m_move_dir.group(4).strip().casefold()
            target_sec = {"प्रार्थना": "prayer", "पक्षकार": "parties", "हस्ताक्षर": "signature_block"}.get(sec_prefix, "pleadings")
            items = draft.get(target_sec) or []
            idx = _para_to_idx(req_num) if target_sec == "pleadings" else (req_num - 1)
            if 0 <= idx < len(items):
                steps = int(step_str) if step_str else 1
                if any(w in dir_word for w in ("सबसे ऊपर", "शुरू", "प्रारंभ", "top")):
                    dest = "top"
                elif any(w in dir_word for w in ("सबसे नीचे", "अंत", "आखिर", "bottom")):
                    dest = "bottom"
                elif "ऊपर" in dir_word or dir_word == "up":
                    dest = "up" if steps == 1 else f"index:{max(0, idx - steps)}"
                else:
                    dest = "down" if steps == 1 else f"index:{min(len(items) - 1, idx + steps)}"
                return {
                    "operation": "move_item",
                    "scope": "targeted",
                    "section": target_sec,
                    "index": idx,
                    "old_text": strip_formatting_markers(str(items[idx]))[:100],
                    "new_text": dest,
                    "reason": f"Move {target_sec} item {req_num} ({dir_word}).",
                }

        # ------------------------------------------------------------------
        # 2. BOLD / UNBOLD (`bold_text` / `unbold_text`)
        # ------------------------------------------------------------------
        is_unbold = any(k in low for k in ("अनबोल्ड", "बोल्ड हटा", "unbold", "सामान्य फ़ॉन्ट"))
        is_bold = is_unbold or any(k in low for k in ("बोल्ड", "bold", "मोटा कर", "डार्क कर"))
        if is_bold and not any(k in low for k in ("बीच में", "लेफ्ट", "राइट", "बाएँ", "दाएँ", "सेंटर", "ऊपर", "नीचे")):
            op_name = "unbold_text" if is_unbold else "bold_text"
            is_global_bold = any(k in low for k in ("पूरे ड्राफ्ट में", "सब जगह", "हर जगह", "everywhere"))

            m_para_bold = re.match(
                r"^(?:सिर्फ\s*|केवल\s*)?(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|धारा|कंडिका|प्रस्तर|para|paragraph)\s*(?:नं०|नंबर|संख्या)?\s*(\d+)\s*"
                r"(?:में\s+['\"“”]?(.+?)['\"“”]?\s*)?(?:को\s*)?(?:अनबोल्ड|बोल्ड|bold|unbold).*$",
                raw_norm,
                flags=re.I,
            )
            if m_para_bold:
                req_num = int(m_para_bold.group(1))
                sub_phrase = (m_para_bold.group(2) or "").strip()
                idx = _para_to_idx(req_num)
                pleadings = draft.get("pleadings") or []
                if 0 <= idx < len(pleadings):
                    return {
                        "operation": op_name,
                        "scope": "targeted",
                        "section": "pleadings",
                        "index": idx,
                        "old_text": sub_phrase,
                        "new_text": f"**{sub_phrase}**" if (sub_phrase and op_name == "bold_text") else sub_phrase,
                        "reason": f"{op_name} in paragraph {req_num}.",
                    }

            m_ctx_bold = re.match(
                r"^['\"“”]?(.+?)['\"“”]?\s+में\s+(?:से\s+)?['\"“”]?(.+?)['\"“”]?\s*(?:शब्द\s*)?(?:को\s*|भी\s*)?(?:अनबोल्ड|बोल्ड|bold|unbold).*$",
                raw_norm,
                flags=re.I,
            )
            if m_ctx_bold:
                ctx_str = m_ctx_bold.group(1).strip()
                target_word = m_ctx_bold.group(2).strip()
                sec_word_clean = re.sub(r"^(?:सिर्फ|केवल|ऊपर|नीचे)\s+", "", ctx_str).strip()
                if sec_word_clean in _SEC_WORD_MAP:
                    target_sec = _SEC_WORD_MAP[sec_word_clean]
                    return {
                        "operation": op_name,
                        "scope": "targeted",
                        "section": target_sec,
                        "index": 0,
                        "old_text": target_word,
                        "new_text": f"**{target_word}**" if op_name == "bold_text" else target_word,
                        "reason": f"{op_name} '{target_word}' in {target_sec}.",
                    }
                matches = count_occurrences_across_draft(draft, ctx_str) or count_occurrences_across_draft(draft, target_word)
                sec_found, idx_found = (matches[0][0], matches[0][1]) if matches else ("parties", 0)
                return {
                    "operation": op_name,
                    "scope": "targeted",
                    "section": sec_found,
                    "index": idx_found,
                    "old_text": target_word,
                    "new_text": f"**{target_word}**" if op_name == "bold_text" else target_word,
                    "reason": f"{op_name} '{target_word}'.",
                }

            m_gen_bold = re.match(
                r"^(?:पूरे\s*ड्राफ्ट\s*में\s+|सब\s*जगह\s+|हर\s*जगह\s+|सिर्फ\s+|केवल\s+|ऊपर\s+|नीचे\s+|मैं\s+बोलूं\s+जैसे\s+|जैसे\s+कि\s+|जैसे\s+)?"
                r"['\"“”]?(.+?)['\"“”]?\s*(?:शब्द\s*|टेक्स्ट\s*|लाइन\s*)?(?:को\s*|भी\s*)?(?:अनबोल्ड|बोल्ड\s*हटा.*|बोल्ड.*|bold.*|unbold.*)$",
                raw_norm,
                flags=re.I,
            )
            if not m_gen_bold:
                m_gen_bold = re.match(
                    r"^(?:make\s+|please\s+)?(?:unbold|bold)\s+['\"“”]?(.+?)['\"“”]?$",
                    raw_norm,
                    flags=re.I,
                )
            if m_gen_bold:
                target_phrase = m_gen_bold.group(1).strip()
                target_phrase = re.sub(r"\s+(?:को|भी|शब्द|टेक्स्ट|लाइन)$", "", target_phrase).strip(" '\"“”")
                if target_phrase in _SEC_WORD_MAP:
                    target_sec = _SEC_WORD_MAP[target_phrase]
                    return {
                        "operation": op_name,
                        "scope": "targeted",
                        "section": target_sec,
                        "index": 0,
                        "old_text": "",
                        "new_text": "",
                        "reason": f"{op_name} entire {target_sec}.",
                    }
                if target_phrase:
                    matches = count_occurrences_across_draft(draft, target_phrase)
                    sec_found, idx_found = (matches[0][0], matches[0][1]) if matches else ("parties" if "बनाम" in target_phrase else "pleadings", 0)
                    return {
                        "operation": op_name,
                        "scope": "global" if is_global_bold else "targeted",
                        "section": sec_found,
                        "index": idx_found,
                        "old_text": target_phrase,
                        "new_text": f"**{target_phrase}**" if op_name == "bold_text" else target_phrase,
                        "reason": f"{op_name} '{target_phrase}'.",
                    }

        # ------------------------------------------------------------------
        # 3. HORIZONTAL ALIGNMENT (`align_text` — लेफ्ट / बाएँ / राइट / दाएँ / बीच में / सेंटर)
        # ------------------------------------------------------------------
        align_dir: str | None = None
        if any(w in low for w in ("लेफ्ट", "बाएँ", "बाएं", "बाईं ओर", "बाईं तरफ", "left side", "left में", "to left", "align left")):
            align_dir = "left"
        elif any(w in low for w in ("राइट में", "राइट साइड", "राइट कर", "दाएँ", "दाएं", "दाईं ओर", "दाईं तरफ", "right side", "to right", "align right")) and not any(
            w in low for w in ("लेफ्ट", "बाएँ", "बाएं", "बीच में", "सेंटर")
        ):
            align_dir = "right"
        elif any(w in low for w in ("बीच में", "बीच मे", "सेंटर", "मध्य में", "बीचों-बीच", "center", "centre")):
            align_dir = "center"
        elif any(w in low for w in ("जस्टिफाई", "justify", "दोनों ओर बराबर")):
            align_dir = "justify"

        if align_dir:
            if any(w in low for w in ("दिनांक", "तारीख", "स्थान")) and not re.search(r"(?:पैरा|पैराग्राफ|सत्यापन)\s*\d*\s*में", low):
                has_place = "स्थान" in low
                has_date = "दिनांक" in low or "तारीख" in low
                target_kw = "स्थान और दिनांक" if (has_place and has_date) else ("स्थान" if has_place else "दिनांक")
                return {
                    "operation": "align_text",
                    "scope": "targeted",
                    "section": "signature_block",
                    "index": 0,
                    "old_text": target_kw,
                    "new_text": align_dir,
                    "reason": f"Align '{target_kw}' in signature_block to {align_dir}.",
                }

            m_para_align = re.match(
                r"^(?:सिर्फ\s*|केवल\s*)?(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|धारा|कंडिका|प्रस्तर|para|paragraph)\s*(?:नं०|नंबर|संख्या)?\s*(\d+)\s*को\s*.*$",
                raw_norm,
                flags=re.I,
            )
            if m_para_align:
                req_num = int(m_para_align.group(1))
                idx = _para_to_idx(req_num)
                pleadings = draft.get("pleadings") or []
                if 0 <= idx < len(pleadings):
                    return {
                        "operation": "align_text",
                        "scope": "targeted",
                        "section": "pleadings",
                        "index": idx,
                        "old_text": "",
                        "new_text": align_dir,
                        "reason": f"Align paragraph {req_num} to {align_dir}.",
                    }

            m_conversational_wrote = re.search(
                r"(?:ऊपर\s+|नीचे\s+|जहाँ\s+|जो\s+)?['\"“”]?([^'\"“”\n]+?)['\"“”]?\s+लिखा\s+है",
                raw_norm,
            )
            extracted_target = ""
            if m_conversational_wrote:
                extracted_target = m_conversational_wrote.group(1).strip()
                extracted_target = re.sub(r"^(?:जैसे\s+कि\s+|जैसे\s+)?(?:ऊपर\s+|नीचे\s+)?", "", extracted_target).strip()
            else:
                m_target_before_ko = re.match(
                    r"^(?:जैसे\s+कि\s+|जैसे\s+)?(?:ऊपर\s+|नीचे\s+|उसी\s+जगह\s+|उस\s+)?['\"“”]?(.+?)['\"“”]?\s*(?:वाले\s*टेक्स्ट\s*|टेक्स्ट\s*|लाइन\s*)?को\s+(?:मैं\s+)?(?:उसी\s+जगह\s+)?(?:लेफ्ट|बाएँ|बाएं|राइट|दाएँ|दाएं|बीच\s*में|सेंटर|मध्य\s*में|center|left|right).*$",
                    raw_norm,
                    flags=re.I,
                )
                if m_target_before_ko:
                    extracted_target = m_target_before_ko.group(1).strip()
                    extracted_target = re.sub(r"^(?:उसी\s+जगह\s+|उस\s+|इस\s+)", "", extracted_target).strip()

            if extracted_target in _SEC_WORD_MAP:
                target_sec = _SEC_WORD_MAP[extracted_target]
                return {
                    "operation": "align_text",
                    "scope": "targeted",
                    "section": target_sec,
                    "index": 0,
                    "old_text": "",
                    "new_text": align_dir,
                    "reason": f"Align {target_sec} to {align_dir}.",
                }

            if extracted_target:
                matches = count_occurrences_across_draft(draft, extracted_target)
                if matches:
                    sec_found, idx_found, _ = matches[0]
                elif "बनाम" in extracted_target:
                    sec_found, idx_found = "parties", 0
                else:
                    sec_found, idx_found = "parties", 0
                return {
                    "operation": "align_text",
                    "scope": "targeted",
                    "section": sec_found,
                    "index": idx_found,
                    "old_text": extracted_target,
                    "new_text": align_dir,
                    "reason": f"Align '{extracted_target}' to {align_dir}.",
                }

        return None

    @classmethod
    def _deterministic_format_and_move_intent(cls, draft: dict[str, Any], instruction: str) -> dict[str, Any] | None:
        """Handle single or multi-part (compound) Bold, Align, Move, and Scoped Replace instructions deterministically."""
        raw = re.sub(r"\s+", " ", str(instruction or "").strip())
        if not raw:
            return None

        parts = re.split(r"\s+(?:और\s+फिर|और\s+मैं\s+बोलूं\s+जैसे|और\s+जैसे|और\s+साथ\s+ही|तथा\s+फिर|और|तथा|फिर)\s+", raw)
        if len(parts) > 1:
            sub_edits: list[dict[str, Any]] = []
            temp_draft = deepcopy(draft)
            editor_instance = cls()
            all_matched = True
            for part in parts:
                p_clean = part.strip(" ,।.")
                if not p_clean:
                    continue
                single = (
                    cls._deterministic_single_format_or_move(temp_draft, p_clean)
                    or cls._deterministic_scoped_replace(temp_draft, p_clean)
                    or cls._deterministic_remove_paragraph(temp_draft, p_clean)
                    or cls._deterministic_layout_intent(p_clean)
                )
                if not single:
                    all_matched = False
                    break
                sub_edits.append(single)
                try:
                    temp_draft = editor_instance._apply_single_op(temp_draft, deepcopy(single))
                except Exception:
                    pass
            if all_matched and len(sub_edits) >= 2:
                first = sub_edits[0]
                return {
                    "operation": "batch",
                    "scope": "multi_target",
                    "section": first.get("section", "parties"),
                    "index": int(first.get("index", 0)),
                    "old_text": first.get("old_text", ""),
                    "new_text": first.get("new_text", ""),
                    "reason": "Multi-step formatting, alignment, or movement instruction.",
                    "edits": sub_edits,
                }

        return cls._deterministic_single_format_or_move(draft, raw)

    @staticmethod
    def _infer_instruction_scope_override(instruction: str) -> str | None:
        """Detect if the user's phrasing explicitly demands 'targeted' or 'global' scope."""
        low = re.sub(r"\s+", " ", str(instruction or "").strip().casefold())
        if any(w in low for w in ("पूरे ड्राफ्ट में", "पूरे दस्तावेज़ में", "सब जगह", "हर जगह", "सभी जगह", "सभी स्थानों पर", "जहाँ-जहाँ", "जहां-जहां", "everywhere", "all places", "throughout")):
            return "global"
        if any(w in low for w in ("सिर्फ", "केवल", "इसी पैरा", "इसी बिंदु", "इसी लाइन", "only in", "particular")) or re.search(
            r"(?:पैराग्राफ|पैरा|बिंदु|पॉइंट|कंडिका|प्रस्तर|शीर्षक|हेडिंग|टाइटल|प्रार्थना|सत्यापन|तस्दीक|हस्ताक्षर|वादी\s*नं|प्रतिवादी\s*नं)\s*[0-9०-९]*\s*में",
            low,
        ):
            return "targeted"
        return None

    @staticmethod
    def _fallback_add_point_deterministic(draft: dict[str, Any], clean: str) -> dict[str, Any]:
        clean_body = re.sub(
            r"^(?:यह\s*पॉइंट\s*जोड़\s*दो\s*(?:कि)?|यह\s*बिंदु\s*जोड़\s*दो\s*(?:कि)?|पॉइंट\s*जोड़ो\s*:?|इसमें\s*यह\s*जोड़ो\s*(?:कि)?|add\s*point\s*:?)\s*",
            "",
            clean,
            flags=re.IGNORECASE,
        ).strip()
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
                "scope": "targeted",
                "section": "pleadings",
                "index": insert_idx,
                "old_text": "",
                "new_text": formatted,
                "reason": "Insert new point before formal closing clauses.",
            }
        return {
            "operation": "append",
            "scope": "targeted",
            "section": "pleadings",
            "index": max(0, len(pleadings) - 1),
            "old_text": "",
            "new_text": formatted,
            "reason": "Append new point to pleadings.",
        }

    def parse_add_point(self, draft: dict[str, Any], instruction: str) -> dict[str, Any]:
        """Use GPT-6.0-Luna to craft a formal, court-grade Hindi pleading/prayer paragraph when the user adds a point."""
        clean = instruction.strip()
        low = clean.casefold()
        if any(k in low for k in ("हटा दो", "हटाओ", "के स्थान पर", "की जगह", "बदल दो", "कोष्ठक में", "बोल्ड", "लेफ्ट", "राइट", "बीच में", "ऊपर कर", "नीचे कर")):
            return self.parse(draft, clean)

        client = self._get_client()
        if client.has_active_key():
            try:
                context = json.dumps({"draft": draft, "editable_sections": self._sections(draft)}, ensure_ascii=False, indent=2)
                res = client.generate_json(
                    system=ADD_POINT_LUNA_SYSTEM,
                    prompt=f"ADVOCATE POINT TO ADD:\n{clean}\n\nCURRENT DRAFT:\n{context}",
                    schema=EDIT_SCHEMA,
                    thinking_level=os.getenv("GEMINI_EDIT_THINKING_LEVEL", "medium"),
                )
                parsed = sanitize_draft_unicode(res)
                if parsed and parsed.get("operation"):
                    parsed.setdefault("scope", "targeted")
                    return parsed
            except Exception as exc:
                log.warning("GPT-6.0-Luna parse_add_point fallback to deterministic: %s", exc)

        return self._fallback_add_point_deterministic(draft, clean)

    def parse_remove_point(self, draft: dict[str, Any], instruction: str) -> dict[str, Any]:
        """Dedicated parser when the user explicitly clicked 'Remove Point'."""
        direct = self._deterministic_remove_paragraph(draft, instruction)
        if direct:
            return direct
        return self.parse(draft, instruction)

    def parse_document_sansodhan(self, draft: dict[str, Any], ocr_text: str, caption: str = "") -> dict[str, Any]:
        """Use Azure Document Intelligence OCR text + GPT-6.0-Luna to apply revisions from an uploaded Photo/PDF/DOCX."""
        context = json.dumps({"draft": draft, "editable_sections": self._sections(draft)}, ensure_ascii=False, indent=2)
        prompt = (
            f"USER_CAPTION_INSTRUCTION:\n{(caption or 'Apply the corrections/additions from the uploaded document to the current draft.').strip()}\n\n"
            f"DOCUMENT_INTELLIGENCE_EXTRACTED_TEXT:\n{ocr_text.strip()}\n\n"
            f"CURRENT DRAFT:\n{context}"
        )
        res = self._get_client().generate_json(
            system=DOCUMENT_SANSODHAN_LUNA_SYSTEM,
            prompt=prompt,
            schema=EDIT_SCHEMA,
            thinking_level=os.getenv("GEMINI_EDIT_THINKING_LEVEL", "high"),
            max_output_tokens=8192,
        )
        parsed = sanitize_draft_unicode(res)
        if caption:
            scope_override = self._infer_instruction_scope_override(caption)
            if scope_override == "targeted" and parsed.get("operation") == "replace_all":
                parsed = convert_edit_scope(draft, parsed, "targeted")
            elif scope_override == "global" and parsed.get("operation") == "replace":
                parsed = convert_edit_scope(draft, parsed, "global")
        return parsed

    def parse(self, draft: dict[str, Any], instruction: str) -> dict[str, Any]:
        direct_fmt_move = self._deterministic_format_and_move_intent(draft, instruction)
        if direct_fmt_move:
            return direct_fmt_move
        direct = self._deterministic_layout_intent(instruction)
        if direct:
            return direct
        direct_rm = self._deterministic_remove_paragraph(draft, instruction)
        if direct_rm:
            return direct_rm
        direct_scoped = self._deterministic_scoped_replace(draft, instruction)
        if direct_scoped:
            return direct_scoped

        context = json.dumps({"draft": draft, "editable_sections": self._sections(draft)}, ensure_ascii=False, indent=2)
        res = self._get_client().generate_json(
            system=SYSTEM,
            prompt=f"USER EDIT INSTRUCTION:\n{instruction.strip()}\n\nCURRENT DRAFT:\n{context}",
            schema=EDIT_SCHEMA,
            thinking_level=os.getenv("GEMINI_EDIT_THINKING_LEVEL", "high"),
            max_output_tokens=8192,
        )
        parsed = sanitize_draft_unicode(res)

        scope_override = self._infer_instruction_scope_override(instruction)
        if scope_override == "targeted" and parsed.get("operation") == "replace_all":
            parsed = convert_edit_scope(draft, parsed, "targeted")
        elif scope_override == "global" and parsed.get("operation") == "replace":
            parsed = convert_edit_scope(draft, parsed, "global")

        if not parsed.get("scope"):
            parsed["scope"] = "global" if parsed.get("operation") == "replace_all" else (
                "multi_target" if parsed.get("operation") == "batch" else "targeted"
            )
        return parsed

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
        scope = str(edit.get("scope", "targeted") or "targeted")
        old = str(edit.get("old_text", ""))
        new = str(edit.get("new_text", ""))
        index = int(edit.get("index", -1))

        new = re.sub(r"\s*कोष्ठक\s+में\s+([^\n,।()]+)", r" (\1)", new).strip()
        if section == "pleadings" and operation in {"insert_before", "insert_after", "append", "rewrite_paragraph"}:
            new = re.sub(
                r"^\s*(?:[*•▪▸]\s*|(?:प्रस्तर|धारा|पैरा|कंडिका)?\s*[0-9०-९]{1,3}\s*(?:\([^)]*\))?\s*[:.\-–—]+\s*)",
                "",
                new,
            ).strip()
        edit["new_text"] = new

        if not self._valid_section(section):
            raise ValueError(f"Invalid edit section: {section}")

        layout_ops = {"page_break_before", "page_break_after", "remove_page_break_before", "remove_page_break_after"}
        text_ops = {"replace", "replace_all", "rewrite_paragraph", "delete", "insert_before", "insert_after", "append"}
        format_move_ops = {"bold_text", "unbold_text", "align_text", "move_item"}
        if operation not in text_ops | layout_ops | format_move_ops:
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

        # ------------------------------------------------------------------
        # BOLD / UNBOLD OPERATIONS (`bold_text` / `unbold_text`)
        # ------------------------------------------------------------------
        if operation in {"bold_text", "unbold_text"}:
            fn = _wrap_bold_substring if operation == "bold_text" else _unwrap_bold_substring
            if scope == "global" and old.strip():
                changed = 0
                for sec_k in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
                    val = str(out.get(sec_k, "") or "")
                    res = fn(val, old)
                    if res is not None and res != val:
                        out[sec_k] = res
                        changed += 1
                for list_k in ("parties", "pleadings", "prayer", "signature_block"):
                    items = out.get(list_k) or []
                    if isinstance(items, list):
                        for i_it, it in enumerate(items):
                            res = fn(str(it), old)
                            if res is not None and res != str(it):
                                items[i_it] = res
                                changed += 1
                        out[list_k] = items
                if not changed and old.strip() != "बनाम":
                    raise ValueError(f"Target text '{old}' not found for {operation}.")
                return sanitize_draft_unicode(out)

            scalar = section in {"opening_averment", "verification", "title", "court_heading", "case_heading"}
            if scalar:
                curr = str(out.get(section, "") or "")
                res = fn(curr, old)
                if res is not None:
                    out[section] = res
                    return sanitize_draft_unicode(out)
            else:
                items = list(out.get(section) or [])
                if items:
                    idx_clamped = max(0, min(index, len(items) - 1))
                    res = fn(str(items[idx_clamped]), old)
                    if res is not None and (not old.strip() or res != str(items[idx_clamped])):
                        items[idx_clamped] = res
                        out[section] = items
                        return sanitize_draft_unicode(out)
                    for i_it, it in enumerate(items):
                        res_i = fn(str(it), old)
                        if res_i is not None and res_i != str(it):
                            edit["index"] = i_it
                            items[i_it] = res_i
                            out[section] = items
                            return sanitize_draft_unicode(out)

            if old.strip():
                matches = count_occurrences_across_draft(out, old)
                if matches:
                    sec_f, idx_f, _ = matches[0]
                    edit["section"] = sec_f
                    edit["index"] = idx_f
                    if sec_f in {"opening_averment", "verification", "title", "court_heading", "case_heading"}:
                        out[sec_f] = fn(str(out.get(sec_f, "")), old) or out[sec_f]
                    else:
                        out[sec_f][idx_f] = fn(str(out[sec_f][idx_f]), old) or out[sec_f][idx_f]
                    return sanitize_draft_unicode(out)
                if old.strip() == "बनाम":
                    return sanitize_draft_unicode(out)

            raise ValueError(f"Target text '{old}' not found in {section} for {operation}.")

        # ------------------------------------------------------------------
        # HORIZONTAL ALIGNMENT OPERATION (`align_text`)
        # ------------------------------------------------------------------
        if operation == "align_text":
            align_raw = str(new or "center").strip().lower()
            if any(k in align_raw for k in ("left", "बाएँ", "बाएं", "लेफ्ट", "बाईं")):
                align_dir = "LEFT"
            elif any(k in align_raw for k in ("right", "दाएँ", "दाएं", "राइट", "दाईं")):
                align_dir = "RIGHT"
            elif any(k in align_raw for k in ("justify", "जस्टिफाई", "बराबर")):
                align_dir = "JUSTIFY"
            else:
                align_dir = "CENTER"
            edit["new_text"] = align_dir.lower()

            if section == "signature_block" or any(k in old.casefold() for k in ("दिनांक", "तारीख", "स्थान")):
                sig_items = [str(x).strip() for x in (out.get("signature_block") or []) if str(x).strip()]
                if not sig_items:
                    sig_items = ["स्थान : ............", "दिनांक : ............", "वादी / प्रार्थी", "द्वारा अधिवक्ता"]

                want_date = any(k in old.casefold() for k in ("दिनांक", "तारीख", "date"))
                want_place = any(k in old.casefold() for k in ("स्थान", "place", "मुकाम", "बमुकाम"))
                if want_date or want_place:
                    new_sig: list[str] = []
                    matched_date = False
                    matched_place = False
                    for ln in sig_items:
                        clean_ln, _ = parse_line_alignment(ln)
                        m_split_role = re.match(
                            r"^((?:दिनांक|तारीख|स्थान)\s*[:\-–—.]*.*?)\s+((?:प्रार्थीगण|प्रार्थी|वादीगण|वादी|प्रतिवादीगण|प्रतिवादी|आपत्तिकर्त्री|शपथकर्ता|शपथग्रहीता|द्वारा\s*अधिवक्ता).*)$",
                            clean_ln,
                        )
                        if m_split_role and ((want_date and ("दिनांक" in clean_ln or "तारीख" in clean_ln)) or (want_place and "स्थान" in clean_ln)):
                            left_part = m_split_role.group(1).strip()
                            right_part = m_split_role.group(2).strip()
                            new_sig.append(_set_line_alignment_marker(left_part, align_dir))
                            new_sig.append(right_part)
                            if "दिनांक" in left_part or "तारीख" in left_part:
                                matched_date = True
                            if "स्थान" in left_part:
                                matched_place = True
                            continue

                        if want_date and any(k in clean_ln for k in ("दिनांक", "तारीख")):
                            new_sig.append(_set_line_alignment_marker(ln, align_dir))
                            matched_date = True
                        elif want_place and "स्थान" in clean_ln:
                            new_sig.append(_set_line_alignment_marker(ln, align_dir))
                            matched_place = True
                        else:
                            new_sig.append(ln)

                    if want_date and not matched_date:
                        new_sig.insert(0, _set_line_alignment_marker("दिनांक : ............", align_dir))
                    if want_place and not matched_place:
                        new_sig.insert(0, _set_line_alignment_marker("स्थान : ............", align_dir))
                    out["signature_block"] = new_sig
                    edit["section"] = "signature_block"
                    return sanitize_draft_unicode(out)

            if "बनाम" in old:
                matches = count_occurrences_across_draft(out, old)
                if matches:
                    sec_f, idx_f, _ = matches[0]
                    edit["section"] = sec_f
                    edit["index"] = idx_f
                    if sec_f in {"opening_averment", "verification", "title", "court_heading", "case_heading"}:
                        val_str = str(out.get(sec_f, "") or "")
                        val_lines = [ln.strip() for ln in val_str.splitlines() if ln.strip()]
                        if len(val_lines) > 1:
                            updated_lines = [
                                _set_line_alignment_marker(ln, align_dir) if _flex_replace_once(ln, old, "__CHK__") is not None else ln
                                for ln in val_lines
                            ]
                            out[sec_f] = "\n".join(updated_lines)
                        else:
                            out[sec_f] = _set_line_alignment_marker(val_str, align_dir)
                    else:
                        out[sec_f][idx_f] = _set_line_alignment_marker(str(out[sec_f][idx_f]), align_dir)
                    return sanitize_draft_unicode(out)

                out["parties"] = [_set_line_alignment_marker(old.strip(), align_dir)]
                edit["section"] = "parties"
                edit["index"] = 0
                return sanitize_draft_unicode(out)

            if old.strip():
                matches = count_occurrences_across_draft(out, old)
                if matches:
                    sec_f, idx_f, _ = matches[0]
                    edit["section"] = sec_f
                    edit["index"] = idx_f
                    if sec_f in {"opening_averment", "verification", "title", "court_heading", "case_heading"}:
                        out[sec_f] = _set_line_alignment_marker(str(out.get(sec_f, "")), align_dir)
                    else:
                        out[sec_f][idx_f] = _set_line_alignment_marker(str(out[sec_f][idx_f]), align_dir)
                    return sanitize_draft_unicode(out)

            scalar = section in {"opening_averment", "verification", "title", "court_heading", "case_heading"}
            if scalar:
                out[section] = _set_line_alignment_marker(str(out.get(section, "")), align_dir)
            else:
                items = list(out.get(section) or [])
                if not items:
                    raise ValueError(f"Section {section} is empty; cannot align.")
                if scope == "global" or index < 0:
                    out[section] = [_set_line_alignment_marker(str(it), align_dir) for it in items]
                else:
                    idx_clamped = max(0, min(index, len(items) - 1))
                    items[idx_clamped] = _set_line_alignment_marker(str(items[idx_clamped]), align_dir)
                    out[section] = items
            return sanitize_draft_unicode(out)

        # ------------------------------------------------------------------
        # PARAGRAPH / ITEM MOVE OPERATION (`move_item`)
        # ------------------------------------------------------------------
        if operation == "move_item":
            if section not in {"pleadings", "prayer", "parties", "signature_block"}:
                raise ValueError(f"move_item is supported on list sections, got '{section}'.")
            items = list(out.get(section) or [])
            if not items:
                raise ValueError(f"Section {section} has no items to move.")
            if old.strip() and (index < 0 or index >= len(items) or _flex_replace_once(str(items[index]), old, "__CHK__") is None):
                found_idx = next((i for i, it in enumerate(items) if _flex_replace_once(str(it), old, "__CHK__") is not None), None)
                if found_idx is not None:
                    index = found_idx
                    edit["index"] = found_idx
            idx_src = max(0, min(index, len(items) - 1))
            dest_spec = str(new or "up").strip().lower()
            if dest_spec in {"up", "-1", "ऊपर"}:
                idx_dst = max(0, idx_src - 1)
            elif dest_spec in {"down", "+1", "नीचे"}:
                idx_dst = min(len(items) - 1, idx_src + 1)
            elif dest_spec in {"top", "first", "सबसे ऊपर", "शुरू", "प्रारंभ"}:
                idx_dst = 0
            elif dest_spec in {"bottom", "last", "सबसे नीचे", "अंत", "आखिर"}:
                idx_dst = len(items) - 1
            elif dest_spec in {"middle", "बीच", "मध्य"}:
                idx_dst = len(items) // 2
            elif dest_spec.startswith("index:") or dest_spec.lstrip("+-").isdigit():
                raw_idx = int(dest_spec.split(":", 1)[1] if ":" in dest_spec else dest_spec)
                idx_dst = max(0, min(len(items) - 1, raw_idx))
            else:
                idx_dst = max(0, idx_src - 1)

            moved_val = items.pop(idx_src)
            items.insert(idx_dst, moved_val)
            out[section] = items
            edit["_from_index"] = idx_src
            edit["_to_index"] = idx_dst
            if not edit.get("old_text"):
                edit["old_text"] = strip_formatting_markers(str(moved_val))[:110]
            return sanitize_draft_unicode(out)

        if operation == "replace_all":
            if not old:
                raise ValueError("replace_all requires old_text.")
            changed = 0
            changed_locations: list[str] = []
            for sec_key in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
                val = str(out.get(sec_key, "") or "")
                replaced = _flex_replace_once(val, old, new)
                if replaced is not None and replaced != val:
                    while True:
                        nxt = _flex_replace_once(replaced, old, new)
                        if nxt is None or nxt == replaced:
                            break
                        replaced = nxt
                    out[sec_key] = _cleanup_party_or_sentence_punctuation(replaced) if not new else replaced
                    changed += 1
                    changed_locations.append(SECTION_LABELS_HI.get(sec_key, sec_key))
            for list_key in ("parties", "pleadings", "prayer", "signature_block"):
                items = out.get(list_key, [])
                if isinstance(items, list):
                    new_items = []
                    for idx_item, it in enumerate(items):
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
                            changed_locations.append(f"{SECTION_LABELS_HI.get(list_key, list_key)} {idx_item + 1}")
                        if s_it.strip():
                            new_items.append(s_it)
                    out[list_key] = new_items
            if not changed:
                raise ValueError(f"Target text '{old}' not found across draft; no change applied.")
            edit["_changed_locations"] = changed_locations
            return sanitize_draft_unicode(out)

        scalar = section in {"opening_averment", "verification", "title", "court_heading", "case_heading"}
        if scalar:
            current = str(out.get(section, "") or "")
            if operation == "rewrite_paragraph":
                if not new.strip():
                    raise ValueError(f"Cannot rewrite {section} with empty text.")
                edit["old_text"] = current
                out[section] = new.strip()
                return sanitize_draft_unicode(out)
            if not current:
                if operation in {"replace", "append", "insert_after"} and new:
                    out[section] = new
                    return sanitize_draft_unicode(out)
                raise ValueError(f"Target section '{section}' is empty.")
            if operation in {"replace", "delete"}:
                replacement = "" if operation == "delete" else new
                updated = _flex_replace_once(current, old, replacement)
                if updated is None:
                    if operation == "replace" and new.strip():
                        edit["old_text"] = current
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
            if operation == "rewrite_paragraph":
                if not new.strip():
                    raise ValueError("Cannot rewrite paragraph with empty text.")
                edit["old_text"] = current
                items[index] = new.strip()
                out[section] = items
                return sanitize_draft_unicode(out)

            if operation in {"replace", "delete"}:
                if operation == "delete" and (not old.strip() or old.strip() == strip_formatting_markers(current)):
                    items.pop(index)
                    out[section] = items
                    return sanitize_draft_unicode(out)
                replacement = "" if operation == "delete" else new

                updated = _flex_replace_once(current, old, replacement)

                if updated is None:
                    found_idx = next((i for i, it in enumerate(items) if _flex_replace_once(str(it), old, replacement) is not None), None)
                    if found_idx is not None:
                        index = found_idx
                        edit["index"] = found_idx
                        updated = _flex_replace_once(str(items[found_idx]), old, replacement)
                    elif operation == "replace" and new.strip() and (len(new.strip()) >= 25 or new.strip().startswith("यह कि")):
                        edit["old_text"] = current
                        updated = new.strip()
                    else:
                        other_matches = count_occurrences_across_draft(out, old)
                        if other_matches:
                            sec_found, idx_found, _ = other_matches[0]
                            edit["section"] = sec_found
                            edit["index"] = idx_found
                            return self._apply_single_op(out, edit)
                        raise ValueError(f"Target text '{old}' not found in {section}.")

                if section == "parties" and operation == "delete" and scope == "global":
                    updated_parties: list[str] = []
                    for party_line in items:
                        s_line = str(party_line)
                        cand = _flex_replace_once(s_line, old, replacement)
                        if cand is not None and cand != s_line:
                            cand = _cleanup_party_or_sentence_punctuation(cand)
                            if cand.strip():
                                updated_parties.append(cand.strip())
                        else:
                            updated_parties.append(s_line)
                    out[section] = updated_parties
                    return sanitize_draft_unicode(out)

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
        old_pleadings_count = len([
            strip_formatting_markers(str(p))
            for p in (draft.get("pleadings") or [])
            if strip_formatting_markers(str(p)) and not strip_formatting_markers(str(p)).startswith("अतिरिक्त कथन")
        ])
        operation = str(edit.get("operation", ""))
        if operation == "batch" and isinstance(edit.get("edits"), list):
            current = deepcopy(draft)
            descriptions = []
            errors = []
            for sub_edit in edit["edits"]:
                try:
                    current = self._apply_single_op(current, sub_edit)
                    descriptions.append(self.describe(sub_edit, draft=draft))
                except Exception as exc:
                    log.warning("Batch sub-edit skipped (%s): %s", sub_edit, exc)
                    errors.append(str(exc))
            if not descriptions and errors:
                raise ValueError("; ".join(errors))
            current = _sync_verification_paragraph_count(current, old_pleadings_count)
            return format_draft_numbers(sanitize_draft_unicode(current)), "\n".join(descriptions)

        updated = self._apply_single_op(draft, edit)
        updated = _sync_verification_paragraph_count(updated, old_pleadings_count)
        return format_draft_numbers(sanitize_draft_unicode(updated)), self.describe(edit, draft=draft)

    @staticmethod
    def describe(edit: dict[str, Any], draft: dict[str, Any] | None = None) -> str:
        if edit.get("operation") == "batch" and isinstance(edit.get("edits"), list):
            lines = [f"{i}. {DraftEditor.describe(sub, draft=draft)}" for i, sub in enumerate(edit["edits"], 1)]
            return "\n".join(lines)

        op = edit.get("operation")
        section = str(edit.get("section", ""))
        idx = int(edit.get("index", 0) or 0)
        old = str(edit.get("old_text", ""))
        new = str(edit.get("new_text", ""))
        target = SECTION_LABELS_HI.get(section, section)
        start_num = int(((draft or {}).get("layout") or {}).get("start_paragraph_number", 1) or 1) if section == "pleadings" else 1
        if section in {"pleadings", "prayer", "parties", "signature_block"} and op not in {"append", "move_item"}:
            display_idx = idx + start_num
            target += f" {display_idx}"

        if op == "bold_text":
            scope_note = " (पूरे ड्राफ्ट में)" if edit.get("scope") == "global" else ""
            disp_txt = old.strip() or f"संपूर्ण {target}"
            return f"🅱️ *{target}*{scope_note} में टेक्स्ट को **बोल्ड (Bold)** किया जाएगा:\nText: **{disp_txt}**"
        if op == "unbold_text":
            disp_txt = old.strip() or f"संपूर्ण {target}"
            return f"🔡 *{target}* से बोल्ड (Bold) हटाकर सामान्य किया जाएगा:\nText: {disp_txt}"
        if op == "align_text":
            align_key = str(new or "center").strip().lower()
            align_hi = ALIGN_LABELS_HI.get(align_key, align_key)
            disp_txt = old.strip() or f"संपूर्ण {target}"
            return f"↔️ *{target}* का स्थान/संरेखण (Alignment) बदलकर *{align_hi}* किया जाएगा:\nText: {disp_txt}"
        if op == "move_item":
            from_i = int(edit.get("_from_index", idx)) + start_num
            to_i = int(edit.get("_to_index", idx)) + start_num
            sec_label = SECTION_LABELS_HI.get(section, section)
            disp_txt = strip_formatting_markers(old)[:95]
            return (
                f"↕️ *{sec_label}* का क्रम बदला जाएगा: *क्रम {from_i} ➔ क्रम {to_i}* "
                f"_(सभी पैराग्राफ स्वतः पुनः क्रमांकित होंगे)_\nText: {disp_txt}"
            )
        if op == "page_break_before":
            return f"📄 {target} को नए पेज से शुरू किया जाएगा।"
        if op == "page_break_after":
            return f"📄 {target} के बाद नया पेज शुरू किया जाएगा।"
        if op == "remove_page_break_before":
            return f"↩️ {target} से पहले का page break हटाया जाएगा।"
        if op == "remove_page_break_after":
            return f"↩️ {target} के बाद का page break हटाया जाएगा।"
        if op == "replace_all":
            locs = edit.get("_changed_locations") or []
            loc_str = f" ({', '.join(locs)})" if locs else ""
            return (
                f"🌐 *स्कोप: पूरे ड्राफ्ट में सभी स्थानों पर बदलाव*{loc_str}\n"
                f"✏️ पूरे ड्राफ्ट में सभी स्थानों पर बदलाव:\nBefore: {old}\nAfter: {new}"
            )
        if op == "append":
            return f"➕ {target} के अंत में नया बिंदु जोड़ा जाएगा:\n{new}"
        if op == "delete":
            return f"🗑️ {target} से यह अंश हटाया जाएगा:\n{old}"
        if op == "replace":
            other_note = ""
            if draft and old:
                all_matches = count_occurrences_across_draft(draft, old)
                if len(all_matches) > 1:
                    other_labels = [lbl for s, i, lbl in all_matches if not (s == section and i == idx)]
                    if other_labels:
                        other_note = (
                            f"\n🎯 *स्कोप:* केवल *{target}* में बदला जा रहा है "
                            f"_(बाकी स्थानों — {', '.join(other_labels[:4])} — को सुरक्षित रखा गया है)_।"
                        )
            return f"✏️ {target} में बदलाव:{other_note}\nBefore: {old}\nAfter: {new}"
        if op == "rewrite_paragraph":
            return f"✏️ {target} में विधिक संशोधन (GPT-6.0-Luna):\nBefore: {old}\nAfter: {new}"
        if op == "insert_before":
            return f"➕ {target} से पहले नया बिंदु जोड़ा जाएगा:\n{new}"
        return f"➕ {target} के बाद नया बिंदु जोड़ा जाएगा:\n{new}"