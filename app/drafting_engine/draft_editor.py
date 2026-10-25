from __future__ import annotations
import json, logging, os, re
from copy import deepcopy
from typing import Any
from .gemini_client import LunaClient
from .krutidev_converter import sanitize_draft_unicode
from .legal_number_formatter import format_draft_numbers

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

SYSTEM = """You are the Senior Judicial Sansodhan (संशोधन) & Scope-Aware Precision Legal Revision Engine powered by Microsoft Foundry GPT-6.0-Luna.

Your job is to understand the advocate's natural-language, voice-dictated, or Document-Intelligence-scanned edit instruction (`USER EDIT INSTRUCTION`) and convert it into a structured edit operation against `CURRENT DRAFT` with **100% accurate Scope Differentiation (`targeted` vs `global` vs `multi_target`)**.

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

3. **MULTI-TARGET SCOPE (`scope="multi_target"`, `operation="batch"`)**:
   - Use `scope="multi_target"` and `operation="batch"` when the advocate asks for changes in 2 or more specific places (e.g., `"शीर्षक और प्रार्थना में ... बदल दो"`, `"पैरा 2 में तारीख बदल दो और पैरा 5 हटा दो"`), placing each targeted change as a separate item in `edits`.

4. **SENIOR COUNSEL LEGAL QUALITY (`new_text`)**:
   - When adding a new point (`insert_before`, `insert_after`, `append`) or rewriting/expanding a paragraph (`rewrite_paragraph`), write rich, authoritative, court-ready Unicode Devanagari Hindi starting with `"यह कि ..."` (without leading paragraph numbers), matching the exact party roles (`वादी`, `प्रतिवादीगण`, `आपत्तिकर्त्री`, `शपथकर्त्री`) of `CURRENT DRAFT`.
   - Format factual numbers in bilingual format (`20,000 (बीस हजार)`).
   - In Dava (`वाद-पत्र`) and Written Statement (`जवाब दावा`), NEVER insert statutory sections (`धारा`), CPC, or BNSS into `pleadings`.

5. **VOICE DICTATION (STT) & DOCUMENT INTELLIGENCE OCR AWARENESS**:
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
        if ch in {"\u093c", "\u200c", "\u200d"}:
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
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"(?:,\s*)?\b\d+\s*(?:\([^)]+\))?\s*[.)]\s*(?=[,;:\-–—]|$)", "", cleaned)
    cleaned = re.sub(r",\s*(?=[\-–—])", " ", cleaned)
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"^\s*[,;:\-–—]+\s*", "", cleaned)
    cleaned = re.sub(r"\s*[,;]+\s*$", "", cleaned)
    cleaned = re.sub(r"\b(?:व|तथा|एवं)\s*(?:व|तथा|एवं)\b", "व", cleaned)
    cleaned = re.sub(r",\s*(?:व|तथा|एवं)\s*,", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if re.match(r"^[\-–—.\s]*(?:वादीगण|वादी|प्रतिवादीगण|प्रतिवादी|विपक्षीगण|विपक्षी|प्रार्थीगण|प्रार्थी|आपत्तिकर्त्री)[\-–—.\s]*$", cleaned):
        return ""
    return cleaned


def _flex_replace_once(source: str, old: str, new: str) -> str | None:
    """Replace `old` in `source`, with whitespace, punctuation, and Devanagari nasal/phonetic flexibility.
    Also cleans up any stale Hindi number word in parentheses immediately following a replaced number
    (e.g., if '412' is replaced with '415' in '412 (चार सौ बारह)', it strips '(चार सौ बारह)' so
    `format_draft_numbers` re-generates '415 (चार सौ पंद्रह)' cleanly!)."""
    if not old:
        return None

    def _post_clean_stale_num_words(prefix: str, suffix: str, old_s: str, new_s: str) -> str:
        # If old_s ended with a number and new_s ends with a different number, strip immediate stale "(हिंदी शब्द)" from suffix
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
        # Save original targeted location so the user can toggle back to targeted if desired
        updated_edit["_orig_section"] = updated_edit.get("section", "pleadings")
        updated_edit["_orig_index"] = int(updated_edit.get("index", 0))
        updated_edit["operation"] = "replace_all"
        updated_edit["scope"] = "global"
        return updated_edit

    # Convert to targeted scope
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
        str(p).strip()
        for p in (draft.get("pleadings") or [])
        if str(p).strip() and not str(p).strip().startswith("अतिरिक्त कथन")
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


def format_draft_section_map(draft: dict[str, Any]) -> str:
    """Build a scannable numbered map of all sections and paragraphs in `draft` so the user can target any area."""
    lines: list[str] = []
    if draft.get("court_heading"):
        lines.append(f"🏛️ *न्यायालय शीर्षक (`court_heading`):* {str(draft['court_heading']).strip()[:110]}")
    if draft.get("case_heading"):
        lines.append(f"📁 *केस शीर्षक (`case_heading`):* {str(draft['case_heading']).strip()[:110]}")
    parties = [str(x).strip() for x in (draft.get("parties") or []) if str(x).strip()]
    if parties:
        lines.append("👥 *पक्षकार (`parties`):*")
        for i, p in enumerate(parties[:8], 1):
            lines.append(f"  • `पक्षकार {i}:` {p[:95]}")
    if draft.get("title"):
        t_flat = str(draft["title"]).strip().replace("\n", " / ")
        lines.append(f"📌 *शीर्षक (`title`):* {t_flat[:130]}")
    if draft.get("opening_averment"):
        lines.append(f"🗣️ *प्रारंभिक कथन (`opening_averment`):* {str(draft['opening_averment']).strip()[:100]}")

    pleadings = [str(x).strip() for x in (draft.get("pleadings") or []) if str(x).strip()]
    if pleadings:
        start_num = int((draft.get("layout") or {}).get("start_paragraph_number", 1) or 1)
        lines.append(f"📋 *पैराग्राफ सूची (`pleadings` — कुल {len(pleadings)}):*")
        p_no = start_num - 1
        for idx, p in enumerate(pleadings[:20], 1):
            if p.startswith("अतिरिक्त कथन"):
                lines.append(f"  🔹 *[उप-शीर्षक — इंडेक्स {idx}]:* `{p}`")
            else:
                p_no += 1
                short = (p[:82].rstrip() + "…") if len(p) > 82 else p
                lines.append(f"  • `पैरा {p_no}` _(सूची क्रम {idx})_: {short}")
        if len(pleadings) > 20:
            lines.append(f"  • _… (+ {len(pleadings) - 20} अन्य पैराग्राफ)_")

    prayer = [str(x).strip() for x in (draft.get("prayer") or []) if str(x).strip()]
    if prayer:
        lines.append(f"⚖️ *प्रार्थना (`prayer` — कुल {len(prayer)} बिंदु):*")
        for i, pr in enumerate(prayer[:6], 1):
            short_pr = (pr[:90].rstrip() + "…") if len(pr) > 90 else pr
            lines.append(f"  • `प्रार्थना {i}:` {short_pr}")

    if draft.get("verification"):
        v_short = str(draft["verification"]).strip().replace("\n", " ")
        lines.append(f"✅ *सत्यापन (`verification`):* {v_short[:115]}…")
    sig = [str(x).strip() for x in (draft.get("signature_block") or []) if str(x).strip()]
    if sig:
        lines.append(f"✍️ *हस्ताक्षर (`signature_block`):* {' | '.join(sig[:4])[:120]}")
    return "\n".join(lines)


def audit_draft_consistency(draft: dict[str, Any], document_type: str = "dava_plaint") -> dict[str, Any]:
    """Run a comprehensive Pre-Filing Legal & Factual Audit on `draft` to detect unfilled blanks (`....`),
    verification paragraph range mismatches, or prohibited statutory citations in pleadings."""
    issues: list[str] = []
    blanks: list[str] = []

    blank_re = re.compile(r"(?:\.{3,}|…+|__{2,}|\[\s*[^\]]*\s*\])")
    for sec in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
        val = str(draft.get(sec, "") or "").strip()
        if val and blank_re.search(val):
            blanks.append(f"• *{SECTION_LABELS_HI.get(sec, sec)}:* `{val[:110]}`")

    for list_sec in ("parties", "pleadings", "prayer", "signature_block"):
        items = draft.get(list_sec) or []
        if isinstance(items, list):
            for idx, it in enumerate(items, 1):
                s_it = str(it or "").strip()
                if not s_it:
                    continue
                # Ignore standard right-aligned role leader dots like ".................... वादी"
                if list_sec == "parties" and re.match(r"^\.{4,}\s*(?:वादी|प्रतिवादी|प्रार्थी|विपक्षी)", s_it):
                    continue
                if blank_re.search(s_it):
                    blanks.append(f"• *{SECTION_LABELS_HI.get(list_sec, list_sec)} {idx}:* `{s_it[:110]}`")

    pleadings = [
        str(p).strip()
        for p in (draft.get("pleadings") or [])
        if str(p).strip() and not str(p).strip().startswith("अतिरिक्त कथन")
    ]
    total_p = len(pleadings)
    ver = str(draft.get("verification") or "").strip()
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

    return {
        "blanks": blanks,
        "issues": issues,
        "total_paragraphs": total_p,
        "is_clean": not blanks and not issues,
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
        """Deterministic fast-path for explicit scoped replacements like:
        - 'सिर्फ पैराग्राफ 3 में X के स्थान पर Y कर दो' -> targeted replace on pleadings[2]
        - 'केवल शीर्षक में X की जगह Y कर दो' -> targeted replace on title
        - 'पूरे ड्राफ्ट में X के स्थान पर Y कर दो' -> global replace_all
        """
        raw = re.sub(r"\s+", " ", str(instruction or "").strip())
        if not raw:
            return None
        raw_norm = raw.translate(str.maketrans("०१२३४५६७८९", "0123456789"))

        # Check explicit global scope pattern: "पूरे ड्राफ्ट में / सब जगह / हर जगह X के स्थान पर / की जगह Y कर दो"
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

        # Check explicit paragraph-targeted replacement: "(सिर्फ/केवल) पैराग्राफ N में X के स्थान पर / की जगह Y कर दो"
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

        # Check explicit section-targeted replacement: "(सिर्फ/केवल) शीर्षक / न्यायालय शीर्षक / केस शीर्षक / सत्यापन / प्रार्थना / हस्ताक्षर में X की जगह Y कर दो"
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
            sec_map = {
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
            target_sec = sec_map.get(sec_word, "title")
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
        if any(k in low for k in ("हटा दो", "हटाओ", "के स्थान पर", "की जगह", "बदल दो", "कोष्ठक में")):
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

        # Enforce explicit user scope intent if GPT-6.0-Luna chose replace_all when user specified a single para/heading, or vice versa
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

        # Auto-convert spoken "कोष्ठक में X" inside new_text if the LLM left it literal
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
                if operation == "delete" and (not old.strip() or old.strip() == current.strip()):
                    items.pop(index)
                    out[section] = items
                    return sanitize_draft_unicode(out)
                replacement = "" if operation == "delete" else new

                # Try replacing in the exact target `index` first (respecting targeted scope!)
                updated = _flex_replace_once(current, old, replacement)

                # Only if `old` was not found at `items[index]`, search other items in `section`
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
                        # Last resort: if old_text is in another section of the draft and scope wasn't strictly locked, locate it
                        other_matches = count_occurrences_across_draft(out, old)
                        if other_matches:
                            sec_found, idx_found, _ = other_matches[0]
                            edit["section"] = sec_found
                            edit["index"] = idx_found
                            return self._apply_single_op(out, edit)
                        raise ValueError(f"Target text '{old}' not found in {section}.")

                # For `parties` deletion when scope is NOT strictly targeted to one index, clean matching role entries if needed
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
            str(p).strip()
            for p in (draft.get("pleadings") or [])
            if str(p).strip() and not str(p).strip().startswith("अतिरिक्त कथन")
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
        if section in {"pleadings", "prayer", "parties", "signature_block"} and op != "append":
            start_num = int(((draft or {}).get("layout") or {}).get("start_paragraph_number", 1) or 1) if section == "pleadings" else 1
            display_idx = idx + start_num
            target += f" {display_idx}"

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