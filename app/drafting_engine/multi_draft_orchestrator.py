from __future__ import annotations
import json, os, re
from pathlib import Path
from typing import Any
from .case_intake import extract_case_facts, reconcile_case_state
from .conversation_state import CaseState, STYLE_NAMES, missing_fields, next_questions
from .gemini_client import GeminiClient
from .hybrid_retriever import HybridCorpusRetriever
from .hindi_corpus_guard import legacy_corruption_score
from .krutidev_converter import sanitize_draft_unicode
from .legal_number_formatter import format_draft_numbers

TYPE_META = {
    "dava_plaint": ("dava", "dava_specialist.txt", "Dava / वाद पत्र"),
    "written_statement": ("written_statement", "written_statement_specialist.txt", "Written Statement / जवाब दावा"),
    "application": ("application", "application_specialist.txt", "Application / प्रार्थना पत्र"),
    "evidence_pw_affidavit": ("evidence_pw_affidavit", "evidence_specialist.txt", "Evidence / साक्ष्य शपथपत्र"),
    "affidavit": ("affidavit", "affidavit_specialist.txt", "Affidavit / शपथपत्र"),
    "legal_notice": ("legal_notice", "legal_notice_specialist.txt", "Legal Notice / विधिक नोटिस"),
    "other_civil": ("other_civil", "other_civil_specialist.txt", "Other Civil Draft / अन्य सिविल ड्राफ्ट"),
}
ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT.parents[1] / "prompts"
SCHEMA = json.loads((ROOT / "draft_schema.json").read_text(encoding="utf-8"))

SECTION_DIVIDER_HEADING = "अतिरिक्त कथन (उजरात मजीद)"

AS_IS_SPECIALIST_PROMPT = """You are the Verbatim Legal Structuring & Handwritten-to-Legal Conversion Engine powered by GPT-6.0-Luna.

The advocate has provided an existing document (printed on A4 paper, PDF, Word, or text) OR a handwritten legal draft (हस्तलिखित ड्राफ्ट) for a Dava (वाद-पत्र), Written Statement (जवाब दावा / प्रतिवाद-पत्र), Affidavit (शपथ-पत्र), Legal Notice (कानूनी नोटिस), Application / Objection Petition (प्रार्थना-पत्र / आपत्ति-पत्र), or other civil filing.

Your goal is to place their document into the structured JSON fields (`court_heading`, `case_heading`, `parties`, `title`, `opening_averment`, `pleadings`, `prayer`, `verification`, `signature_block`) so the bot's deterministic renderer outputs a court-ready **Legal-Size (`8.5" × 14"`)** DOCX and PDF that they can immediately use or edit.

CRITICAL RULES FOR "AS-IT-IS / HANDWRITTEN TO LEGAL STRUCTURE" MODE:
1. **100% Fidelity to the Advocate's Source Document (`RAW_SOURCE_DOCUMENT_TEXT`)**:
   - Preserve every single paragraph, statement, party name, father/husband name, age, address, Gata/Khasra number, area, boundary (चौहद्दी), date, amount, prayer clause, and verification written in `RAW_SOURCE_DOCUMENT_TEXT` **AS-IT-IS (हूबहू / ज्यों का त्यों)** in the exact same order.
   - NEVER add external template paragraphs, NEVER invent new facts, and NEVER summarize or shorten the advocate's paragraphs.
2. **If the Source is an Already-Structured A4 / Typed Document**:
   - Keep the exact wording and structure of each section (`court_heading`, `case_heading`, `parties`, `title`, `opening_averment`, `pleadings`, `prayer`, `verification`, `signature_block`) as written in the A4 document.
   - Strip only leading paragraph numbers (`1.`, `2.`, `1 (एक) :`, `*`) from items in `pleadings` because the renderer automatically numbers them `1 (एक) : `, `2 (दो) : `, etc.
3. **If the Source is a Handwritten Draft or Rough Notes (हस्तलिखित ड्राफ्ट)**:
   - Silently fix any OCR character/matra recognition noise caused by handwriting scan (e.g. broken Devanagari matras) using Indian legal context while preserving the advocate's exact intended words and facts.
   - Organize the handwritten content into proper Indian court legal structure:
     * `court_heading`: The Court or Authority name (`न्यायालय श्रीमान ...` or `समक्ष : ...`).
     * `case_heading`: Case/Suit number line (`मूल वाद संख्या ... सन् ...`), or `""` for non-court affidavits/legal notices.
     * `parties`: Each plaintiff and defendant (or sender/recipient) in proper cause-title format.
     * `title`: Proper centered document title (`वाद-पत्र ...`, `प्रतिवाद-पत्र / जवाब दावा`, `शपथ-पत्र`, `रजिस्टर्ड कानूनी नोटिस`, `प्रार्थना-पत्र ...`, `आपत्ति-पत्र ...`).
     * `opening_averment`: Formal opening line (`उपरोक्त वादी निम्नलिखित निवेदन करता है :-` / `मान्यवर, ...` / `मैं शपथग्रहीता ... शपथपूर्वक बयान करता हूँ :-`).
     * `pleadings`: Each handwritten point/paragraph formatted cleanly starting with `"यह कि ..."` (without leading paragraph numbers).
     * `prayer`: Prayer clauses (`प्रार्थना`) if present or applicable (leave `[]` for affidavits).
     * `verification`: Formal verification (`सत्यापन` / `तस्दीक`) without repeating the heading word `'सत्यापन'`.
     * `signature_block`: Place, date, party/deponent signature line, and advocate line.
"""

SINGLE_PAGE_SPECIALIST_PROMPT = """You are the Single-Page Legal Recreation & Correction Engine powered by GPT-6.0-Luna.

The advocate has an existing multi-page case file where a specific single page (e.g., Page 1, Page 2, Page 3, or Page 4) has a typo in someone's name, father's name, year, date, Gata number, or paragraph wording, and wants to **recreate ONLY that exact single page** with the requested correction (`CORRECTION_INSTRUCTION`) so they can print and replace just that page in the physical file.

CRITICAL RULES FOR SINGLE-PAGE RECREATION MODE:
1. **Recreate ONLY the Content Present on the Target Page (`TARGET_PAGE_RAW_TEXT` / `STRUCTURAL_PAGE_PARSE`)**:
   - Do NOT invent a `court_heading`, `case_heading`, `parties`, or `title` if the target page is a continuation page (like Page 2 or Page 3) that does not have those headings! Leave `court_heading: ""`, `case_heading: ""`, `parties: []`, `title: ""` if they are not on this page.
   - Do NOT invent `prayer`, `verification`, or `signature_block` if they do not appear on this page! Leave them empty (`[]` or `""`) if they are on a later page.
2. **Preserve Exact Starting Paragraph Number & Top Continuation**:
   - Keep `layout` with `"single_page_mode": True`, `"single_page_number": TARGET_PAGE_NUMBER`, `"start_paragraph_number": START_PARAGRAPH_NUMBER`, and `"page_top_continuation": PAGE_TOP_CONTINUATION`.
   - Strip leading paragraph numbers (`9.`, `९ (नौ) :`, etc.) from items in `pleadings` because the renderer automatically numbers them starting from `START_PARAGRAPH_NUMBER` (`9 (नौ) : `, `10 (दस) : `, etc.).
3. **Apply the Requested Correction (`CORRECTION_INSTRUCTION`) Precisely**:
   - If `CORRECTION_INSTRUCTION` specifies a change (e.g., changing a name, year, date, Gata number, or wording in a specific paragraph or across this page), apply it cleanly in formal court Hindi with bilingual number formatting (`2025 (दो हजार पच्चीस)`).
   - Preserve all other paragraphs and sentences on this page 100% verbatim.
"""

BASE_RULES = [
    "CASE_FACTS are the sole authority for case-specific names, dates, plot/Gata numbers, roll/registration numbers, boundaries, and events. Never copy old party names or unrelated past transactions from retrieved templates.",
    "STRICT PLEADING RULE (ORDER VI RULE 2 CPC — NO LEGAL SECTIONS / NO CPC / NO BNSS IN PLEADINGS): In Written Statements (जवाब दावा / प्रतिवाद-पत्र) and Plaints (वाद-पत्र / Dava), citing legal Sections ('धारा' of any Act), Orders/Rules ('आदेश ... नियम ...'), CPC ('सिविल प्रक्रिया संहिता' / 'सी०पी०सी०' / 'जाप्ता दीवानी'), BNSS ('भारतीय नागरिक सुरक्षा संहिता' / 'बी०एन०एस०एस०'), BNS ('भारतीय न्याय संहिता'), IPC ('भारतीय दंड संहिता'), CrPC ('दंड प्रक्रिया संहिता'), Specific Relief Act ('विशिष्ट अनुतोष अधिनियम'), UP Revenue Code ('उत्तर प्रदेश राजस्व संहिता'), Evidence Act ('साक्ष्य अधिनियम'), or Limitation Act ('परिसीमा अधिनियम') is STRICTLY PROHIBITED. Pleadings must state ONLY material facts (सारभूत तथ्य). (Note: Writing 'वादपत्र की धारा 1 (एक)', 'वादपत्र की धारा 2 (दो)' to refer to Paragraph 1, Paragraph 2 of the Plaint in a Written Statement is standard Hindi court terminology for paragraph numbers and is required in Part 1 of a Written Statement).",
    "CAUSE-TITLE PARTY FORMATTING (`parties`): Never write 'वादी:', 'वादीगण:', 'प्रतिवादी:', or 'प्रतिवादीगण:' at the start of any party line (the renderer places `.................... वादी` and `.................... प्रतिवादीगण` on the right automatically). Never merge multiple plaintiffs or multiple defendants into a single string—put each person as a separate item in `parties` so they are numbered individually (`1 (एक). ...`, `2 (दो). ...`).",
    "MANDATORY BILINGUAL NUMBER FORMAT: Every factual numerical value (Gata/Khasra/Bhoomi number, area in hectare/biswa/bigha/decimal, age, date, time, money amount, fraction, and Vaad Patra paragraph number) MUST be written in digits followed by its Hindi words in parentheses, e.g., 'वादपत्र की धारा 1 (एक)', 'गाटा संख्या 412 (चार सौ बारह)', 'रकबा 0.250 (शून्य दशमलव दो पाँच शून्य) हेक्टेयर', '2 (दो) बिस्वा', 'उम्र लगभग 52 (बावन) वर्ष', 'दिनांक 12 अक्टूबर 2026 (बारह अक्टूबर दो हजार छब्बीस)', 'प्रातः 10:00 (दस) बजे'. Never write bare number words like 'दो बिस्वा' without the digit '2 (दो) बिस्वा'.",
    "NEVER write thin, 1-sentence summaries. Every pleading or affidavit paragraph must be written in rich, authoritative, multi-sentence formal Hindi ('यह कि ...').",
    "FOR WRITTEN STATEMENT (जवाब दावा) TWO-PART STRUCTURE: (1) In Part 1 (para-wise replies to the Vaad Patra), reply strictly 1-to-1 to the numbered paragraphs of the Vaad Patra ('यह कि वादपत्र की धारा 1 (एक) का कथन...', 'यह कि वादपत्र की धारा 2 (दो) का कथन...', etc.) on purely factual grounds, and NEVER put a 'भाग प्रथम' heading before Paragraph 1. (2) Immediately after the para-wise replies, insert the exact section heading item 'अतिरिक्त कथन (उजरात मजीद)', followed by continuously numbered defense paragraphs setting out the defendants' true factual case, absence of cause of action, peshbandi/harassment, suppression of material facts/unclean hands, undervaluation/insufficient court fee, and prayer for dismissal with special costs — WITHOUT citing any legal sections, CPC Orders/Rules, BNSS, or statutory Acts.",
    "Preserve event status exactly: attempt is not completion; threat is not dispossession; allegation is not an established fact.",
    "If CASE_FACTS contains custom_add_points, include every requested point as a proper paragraph/clause.",
    "If CASE_FACTS contains custom_remove_points, omit those topics/clauses from the draft.",
    "Never number paragraphs yourself. The deterministic renderer assigns continuous numbering.",
    "Return only JSON matching the supplied schema.",
]

_PARTY_STOPWORDS = {
    "वादी", "वादीगण", "वादिनी", "प्रतिवादी", "प्रतिवादीगण", "प्रतिवादिनी", "विपक्षी", "विपक्षीगण",
    "प्रार्थी", "प्रार्थीगण", "शपथकर्ता", "शपथग्रहीता", "उम्र", "आयु", "करीब", "लगभग", "वर्ष",
    "पुत्र", "पुत्री", "पत्नी", "पति", "विधवा", "स्व", "स्व०", "स्वर्गीय", "श्री", "श्रीमती",
    "सुश्री", "कुमारी", "निवासी", "निवासीगण", "मूल", "वर्तमान", "पता", "ग्राम", "मौजा", "मौज़ा",
    "डाकखाना", "पोस्ट", "परगना", "तहसील", "जिला", "थाना", "वार्ड", "संख्या", "मकान", "नंबर", "बनाम",
    "plaintiff", "plaintiffs", "defendant", "defendants", "respondent", "applicant",
    "petitioner", "deponent", "sender", "recipient", "age", "about", "approx", "years",
    "son", "daughter", "wife", "husband", "widow", "late", "shri", "smt", "resident",
    "village", "post", "tehsil", "pargana", "district", "ward", "house",
}

_ROLE_WORDS_PAT = (
    r"(?:प्रतिवादीगण|प्रतिवादिनीगण|प्रतिवादिनी|प्रतिवादी|विपक्षीगण|विपक्षी|"
    r"वादीगण|वादिनी|वादी|प्रार्थीगण|प्रार्थिनी|प्रार्थी|"
    r"आवेदकगण|आवेदिका|आवेदक|आपत्तिकर्त्री|आपत्तिकर्तागण|आपत्तिकर्ता|"
    r"उत्तरदातागण|उत्तरदाता|शपथग्रहीता|शपथग्रहीत्री|शपथकर्त्री|शपथकर्ता)"
    r"(?:\s*/\s*(?:प्रतिवादीगण|प्रतिवादिनीगण|प्रतिवादिनी|प्रतिवादी|विपक्षीगण|विपक्षी|"
    r"वादीगण|वादिनी|वादी|प्रार्थीगण|प्रार्थिनी|प्रार्थी|"
    r"आवेदकगण|आवेदिका|आवेदक|आपत्तिकर्त्री|आपत्तिकर्तागण|आपत्तिकर्ता|"
    r"उत्तरदातागण|उत्तरदाता|शपथग्रहीता|शपथकर्ता))*"
)

_LEADING_ROLE_PREFIX_RE = re.compile(
    rf"^\s*(?:(?:\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*)?"
    rf"{_ROLE_WORDS_PAT}"
    rf"(?:\s*(?:संख्या|सं०|सं\.|नं०|नं\.)\s*\d+(?:\s*\([^)]+\))?(?:\s*(?:व|एवं|तथा)\s*\d+(?:\s*\([^)]+\))?)?)?"
    rf"\s*[:\-–—]+\s*)+"
)

_TRAILING_ROLE_SUFFIX_RE = re.compile(
    rf"\s*(?:[,;|\-–—.…]+\s*)?"
    rf"{_ROLE_WORDS_PAT}"
    rf"(?:\s*(?:संख्या|सं०|सं\.|नं०|नं\.)\s*\d+(?:\s*\([^)]+\))?(?:\s*(?:व|एवं|तथा)\s*\d+(?:\s*\([^)]+\))?)?)?"
    rf"[।.]?\s*$"
)


def _flatten_str_item(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for k in ("text", "content", "party", "line", "clause", "paragraph", "name", "value"):
            if item.get(k) and isinstance(item[k], str):
                return item[k].strip()
        vals = [str(v).strip() for v in item.values() if isinstance(v, (str, int, float)) and str(v).strip()]
        return " ".join(vals).strip()
    return str(item or "").strip()


def _strip_party_role_affixes(line: str) -> str:
    """Remove both leading ('वादी: ...', 'प्रतिवादीगण: ...') and trailing ('... — वादी', '... प्रार्थीगण / आवेदकगण') role labels."""
    s = str(line or "").strip()
    s = _LEADING_ROLE_PREFIX_RE.sub("", s).strip()
    s = _TRAILING_ROLE_SUFFIX_RE.sub("", s).strip()
    return s


def _extract_trailing_party_role(line: str) -> str:
    """Extract custom role label from a party line if present (e.g. 'प्रार्थीगण / आवेदकगण' or 'आपत्तिकर्त्री / प्रतिवादिनी')."""
    s = str(line or "").strip()
    m = re.search(rf"(?:[—–\-…]|\.\.\.)+\s*({_ROLE_WORDS_PAT})\s*[।.]?\s*$", s)
    if m:
        return m.group(1).strip()
    return ""


def _expand_merged_party_lines(lines: list[str]) -> list[str]:
    """Split a merged compound party line like 'दिनेश कुमार यादव पुत्र रामेश्वर यादव तथा संतोष यादव पुत्र रामेश्वर यादव, दोनों निवासीगण ग्राम भसौरा...' into individual party lines."""
    expanded: list[str] = []
    for line in lines:
        s = _strip_party_role_affixes(line)
        if not s:
            continue
        s_no_num = re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", s).strip()
        m = re.match(
            r"^(?P<p1>.+?(?=\s)(?:पुत्र|पत्नी|पुत्री|विधवा)\s+[^,]+?)\s+(?:तथा|एवं|व|और)\s+"
            r"(?P<p2>.+?(?=\s)(?:पुत्र|पत्नी|पुत्री|विधवा)\s+[^,]+?)\s*,?\s*"
            r"(?:दोनों\s+|तीनों\s+|सभी\s+)?(?:निवासीगण|निवासी)\s+(?P<addr>.+)$",
            s_no_num,
        )
        if m:
            p1 = m.group("p1").strip(" ,")
            p2 = m.group("p2").strip(" ,")
            addr = m.group("addr").strip()
            expanded.append(f"{p1}, निवासी {addr}")
            expanded.append(f"{p2}, निवासी {addr}")
        else:
            expanded.append(s)
    return expanded


def _format_cause_title_parties(parties: list[str], facts: dict[str, Any] | None = None) -> list[str]:
    from .renderers.legal_document_renderer import split_party_blocks
    pl, df, other = split_party_blocks(parties)

    facts = facts or {}
    fact_pls = [_strip_party_role_affixes(str(x)) for x in (facts.get("plaintiffs") or []) if _strip_party_role_affixes(str(x))]
    fact_dfs = [_strip_party_role_affixes(str(x)) for x in (facts.get("defendants") or []) if _strip_party_role_affixes(str(x))]
    fact_pls = _expand_merged_party_lines(fact_pls)
    fact_dfs = _expand_merged_party_lines(fact_dfs)

    cleaned_pl = _expand_merged_party_lines(pl)
    cleaned_df = _expand_merged_party_lines(df)

    if len(fact_pls) > len(cleaned_pl):
        cleaned_pl = fact_pls
    if len(fact_dfs) > len(cleaned_df):
        cleaned_df = fact_dfs

    if not cleaned_pl and not cleaned_df:
        return parties

    out: list[str] = []
    if cleaned_pl:
        if len(cleaned_pl) > 1:
            numbered_pl = []
            for i, p in enumerate(cleaned_pl, 1):
                p_clean = re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", p).strip()
                numbered_pl.append(f"{i}. {p_clean}")
            out.extend(numbered_pl)
            out.append(".................... वादीगण")
        else:
            p_clean = re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", cleaned_pl[0]).strip()
            is_plural_pl = any(w in p_clean for w in ("दोनों", "तीनों", "निवासीगण", "पुत्रगण", "वादीगण", "आदि"))
            out.append(p_clean)
            out.append(".................... वादीगण" if is_plural_pl else ".................... वादी")

    if cleaned_df:
        if len(cleaned_df) > 1:
            numbered_df = []
            for i, d in enumerate(cleaned_df, 1):
                d_clean = re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", d).strip()
                numbered_df.append(f"{i}. {d_clean}")
            out.extend(numbered_df)
            out.append(".................... प्रतिवादीगण")
        else:
            d_clean = re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", cleaned_df[0]).strip()
            is_plural_df = any(w in d_clean for w in ("दोनों", "तीनों", "निवासीगण", "पुत्रगण", "प्रतिवादीगण", "आदि"))
            out.append(d_clean)
            out.append(".................... प्रतिवादीगण" if is_plural_df else ".................... प्रतिवादी")

    for o in other:
        o_clean = _strip_party_role_affixes(o)
        if o_clean:
            out.append(o_clean)
    return out


def _clean_short_name(raw: str) -> str:
    s = _strip_party_role_affixes(str(raw or ""))
    s = re.sub(r"^\s*मैं\s*,?\s*(?:शपथग्रहीता|शपथग्रहीत्री|शपथकर्त्री|शपथकर्ता)?\s*", "", s).strip()
    m = re.split(r"\s+(?:पुत्र|पत्नी|पुत्री|विधवा|उम्र|आयु|निवासी)(?=\s|$)|,", s, maxsplit=1)
    if m and m[0].strip():
        return re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", m[0].strip(" ()"))
    return s.strip(" ()")


def _extract_party_names_for_doc(facts: dict[str, Any], document_type: str = "dava_plaint") -> tuple[str, str]:
    """Return (short_party_names_str, role_label_str) appropriate for the document_type."""
    if document_type in {"affidavit", "evidence_pw_affidavit"}:
        dep = facts.get("deponent")
        if isinstance(dep, list) and dep:
            dep = dep[0]
        if isinstance(dep, str) and dep.strip():
            short = _clean_short_name(dep)
            if short:
                return short, "उपरोक्त शपथग्रहीता / शपथकर्ता"
        for key in ("plaintiffs", "parties", "sender"):
            val = facts.get(key)
            if isinstance(val, list) and val:
                short = _clean_short_name(str(val[0]))
                if short:
                    return short, "उपरोक्त शपथग्रहीता / शपथकर्ता"
            elif isinstance(val, str) and val.strip():
                short = _clean_short_name(val)
                if short:
                    return short, "उपरोक्त शपथग्रहीता / शपथकर्ता"
        return "शपथग्रहीता", "उपरोक्त शपथग्रहीता / शपथकर्ता"

    if document_type == "written_statement":
        defs = facts.get("defendants") or []
        if isinstance(defs, str):
            defs = [defs]
        defs = _expand_merged_party_lines([str(d) for d in defs])
        names = [_clean_short_name(d) for d in defs if _clean_short_name(d)]
        if len(names) > 1:
            return " एवं ".join(names), "उपरोक्त प्रतिवादीगण"
        if len(names) == 1:
            return names[0], "उपरोक्त प्रतिवादी"
        return "प्रतिवादीगण", "उपरोक्त प्रतिवादीगण"

    for key in ("plaintiffs", "deponent", "sender", "parties"):
        val = facts.get(key)
        if isinstance(val, list) and val:
            expanded = _expand_merged_party_lines([str(x) for x in val])
            names = [_clean_short_name(x) for x in expanded if _clean_short_name(x)]
            if names:
                role = "उपरोक्त वादीगण" if len(names) > 1 else "उपरोक्त वादी"
                return " एवं ".join(names), role
        elif isinstance(val, str) and val.strip():
            return _clean_short_name(val), "उपरोक्त वादी"
    return "वादी", "उपरोक्त वादी"


_DEVANAGARI_DIGITS_TRANS = str.maketrans("०१२३४५६७८९", "0123456789")


def _norm_dedup_key(s: str) -> str:
    """Normalize paragraph text for duplicate-page detection across Hindi/ASCII digits and punctuation."""
    t = str(s or "").translate(_DEVANAGARI_DIGITS_TRANS)
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"[^0-9\u0900-\u097F]+", "", t)
    return t[:90]


def parse_structured_legal_document(raw_text: str, document_type: str = "") -> dict[str, Any]:
    """Deterministically parse a structured Hindi legal document (from PDF, DOCX, OCR, or multi-part pasted text)
    into the canonical draft schema (`court_heading`, `case_heading`, `parties`, `title`, `opening_averment`,
    `pleadings`, `prayer`, `verification`, `signature_block`) with zero LLM token truncation or latency.
    """
    text = str(raw_text or "").strip()
    if not text:
        return {}

    # 1. Remove page footer/header artifacts like "प्रकीर्ण वाद संख्या ६४/२०२५ — मंजूलता आदि बनाम राजरानी पृष्ठ १ / २"
    cleaned_lines: list[str] = []
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if re.search(r"पृष्ठ\s*[0-9०-९]+\s*/\s*[0-9०-९]+", ln):
            ln = re.sub(r"^.*?पृष्ठ\s*[0-9०-९]+\s*/\s*[0-9०-९]+\s*$", "", ln).strip()
            if not ln:
                continue
        if re.match(r"^page\s*\d+(?:\s*of\s*\d+)?$", ln, flags=re.I):
            continue
        if re.match(r"^[.\-_—–]{4,}$", ln):
            continue
        if re.match(r"^[0-9०-९]{1,3}\s*[.):\-–—]*$", ln):
            continue
        cleaned_lines.append(ln)

    # 2. Split lines that contain multiple inline bulleted/numbered "यह कि" points
    expanded_lines: list[str] = []
    for ln in cleaned_lines:
        parts = re.split(
            r"(?=(?:^|\s)(?:[*•▪▸]\s*|(?:प्रस्तर|धारा|पैरा)?\s*[0-9०-९]{1,3}\s*(?:\([^)]+\))?\s*[.):\-–—]\s*)यह\s+कि(?=\s))",
            ln,
        )
        for p in parts:
            p_str = p.strip()
            if p_str:
                expanded_lines.append(p_str)

    court_heading = ""
    case_heading = ""
    parties: list[str] = []
    title_lines: list[str] = []
    opening_averment = ""
    pleadings: list[str] = []
    prayer: list[str] = []
    verification_lines: list[str] = []
    signature_block: list[str] = []

    section = "header"
    seen_versus = False
    para_start_re = re.compile(
        r"^(?:[*•▪▸\-]\s*|(?:प्रस्तर|धारा|पैरा)?\s*[0-9०-९]{1,3}\s*(?:\([^)]*\))?\s*[.):\-–—]+\s*)?(यह\s+कि(?=\s|:).*)$"
    )
    numbered_item_re = re.compile(
        r"^(?:[*•▪▸]\s*|[0-9०-९]{1,3}\s*(?:\([^)]*\))?\s*[.):\-–—]+\s*)(.+)$"
    )

    for ln in expanded_lines:
        # If we already finished collecting prayer + advocate signature on an earlier page
        # and encounter a repeated paragraph from a duplicate page in the PDF, stop!
        if (
            section in {"prayer", "verification", "signature"}
            and prayer
            and any("अधिवक्ता" in s or "एडवोकेट" in s for s in signature_block)
            and para_start_re.match(ln)
        ):
            break

        # Detect section transitions
        if re.match(r"^[\s*•_\-–—]*(?:प्रार्थना|अनुतोष)(?:\s*\(Prayer\))?\s*[:\-–—]*\s*$", ln, flags=re.I):
            section = "prayer"
            continue
        m_prayer_inline = re.match(r"^[\s*•_\-–—]*(?:प्रार्थना|अनुतोष)\s*[:\-–—]+\s*(.+)$", ln)
        if m_prayer_inline:
            section = "prayer"
            prayer.append(m_prayer_inline.group(1).strip())
            continue
        if section in {"pleadings", "header"} and re.match(
            r"^(?:अतः|अतएव)\s+(?:माननीय\s+न्यायालय|श्रीमान\s+जी|न्यायालय)\s+से\s+(?:सादर\s+|सविनय\s+|विनम्र\s+)?प्रार्थना\s+है",
            ln,
        ):
            section = "prayer"
            prayer.append(ln)
            continue

        if re.match(r"^[\s*•_\-–—]*(?:सत्यापन|तस्दीक)(?:\s*\(Verification\))?\s*[:\-–—]*\s*$", ln, flags=re.I):
            section = "verification"
            continue
        m_ver_inline = re.match(r"^[\s*•_\-–—]*(?:सत्यापन|तस्दीक)(?:\s*\(Verification\))?\s*[:\-–—]+\s*(.+)$", ln, flags=re.I)
        if m_ver_inline:
            section = "verification"
            verification_lines.append(m_ver_inline.group(1).strip())
            continue
        if section in {"pleadings", "prayer"} and re.match(
            r"^(?:मैं|हम)\s*,?\s*(?:उपरोक्त|शपथग्रहीता|शपथग्रहीत्री|शपथकर्त्री|शपथकर्ता|वादी|प्रतिवादी|आपत्तिकर्त्री|प्रार्थी).*(?:सत्यापित|तस्दीक)\s+कर",
            ln,
        ):
            section = "verification"
            verification_lines.append(ln)
            continue

        if section in {"pleadings", "prayer", "verification", "signature"} and (
            re.match(
                r"^(?:दिनांक|स्थान|तारीख|हस्ताक्षर|पहचानकर्ता|द्वारा\s*अधिवक्ता|अधिवक्ता|एडवोकेट|प्रार्थीगण|प्रार्थी|आवेदकगण|आपत्तिकर्त्री|आपत्तिकर्ता|वादीगण|वादी|प्रतिवादीगण|प्रतिवादी|शपथग्रहीता|शपथग्रहीत्री|शपथकर्त्री|शपथकर्ता|मो०|मोबाइल|पंजीकरण\s*सं)(?=\s|$|[:।.,;\-–—(])",
                ln,
            )
            or (section == "signature" and len(ln) <= 65)
        ):
            section = "signature"
            signature_block.append(ln)
            continue

        m_para = para_start_re.match(ln)
        if m_para and section in {"header", "pleadings"}:
            section = "pleadings"
            pleadings.append(m_para.group(1).strip())
            continue

        if section == "header":
            if not court_heading and re.match(r"^(?:न्यायालय|माननीय\s+न्यायालय|समक्ष|सेवा\s+में)(?=\s|$)", ln):
                court_heading = ln
                continue
            if not case_heading and re.search(
                r"(?:वाद\s*संख्या|प्रकीर्ण\s*वाद|मूल\s*वाद|मुकदमा\s*सं|अपील\s*सं|केस\s*नं|वाद\s*सं०|सन्\s*[0-9०-९]{4})",
                ln,
            ):
                case_heading = ln
                continue
            if re.match(r"^बनाम\s*[:\-–—]*$", ln) or ln == "बनाम":
                seen_versus = True
                if not parties and title_lines and len(title_lines[-1]) <= 95:
                    parties.append(title_lines.pop())
                continue
            if " बनाम " in ln and not re.search(
                r"(?:प्रार्थना\s*पत्र|आपत्ति\s*पत्र|वाद\s*पत्र|जवाब\s*दावा|शपथ\s*पत्र|नोटिस)",
                ln,
            ):
                parties.append(ln)
                seen_versus = True
                continue
            is_role_only = bool(re.match(
                r"^(?:\.\.\.|…|—|–|-)+\s*(?:प्रार्थी|आवेदक|वादी|प्रतिवादी|आपत्तिकर्त्री|आपत्तिकर्ता|विपक्षी|उत्तरदाता)",
                ln,
            ))
            if is_role_only:
                if parties and not re.search(r"(?:\.\.\.|…|—)\s*(?:प्रार्थी|आवेदक|वादी|प्रतिवादी|आपत्तिकर्त्री|आपत्तिकर्ता|विपक्षी|उत्तरदाता)", parties[-1]):
                    parties[-1] = f"{parties[-1]} {ln}".strip()
                else:
                    parties.append(ln)
                continue
            if re.search(
                r"(?:\.\.\.|…|—|–|-)\s*(?:प्रार्थी|आवेदक|वादी|प्रतिवादी|आपत्तिकर्त्री|आपत्तिकर्ता|विपक्षी|उत्तरदाता)",
                ln,
            ) or (
                not title_lines
                and any(
                    ln.endswith(r_sfx)
                    for r_sfx in (
                        "प्रार्थीगण", "आवेदकगण", "वादी", "वादीगण", "वादिनी",
                        "प्रतिवादी", "प्रतिवादीगण", "प्रतिवादिनी", "आपत्तिकर्त्री",
                        "विपक्षीगण", "उत्तरदातागण",
                    )
                )
            ):
                parties.append(ln)
                continue
            if not title_lines and len(ln) <= 95 and not re.search(
                r"(?:प्रार्थना\s*पत्र|आपत्ति\s*पत्र|वाद\s*पत्र|जवाब\s*दावा|शपथ\s*पत्र|नोटिस|मान्यवर|महोदय|विरुद्ध)",
                ln,
            ):
                if parties and re.match(r"^(?:\.\.\.|…|—|–|-)+\s*", parties[-1]):
                    parties[-1] = f"{ln} {parties[-1]}".strip()
                    continue
                if seen_versus and len(parties) == 1:
                    parties.append(ln)
                    continue
            if re.match(
                r"^(?:मान्यवर|महोदय|श्रीमान\s*जी|उपरोक्त\s*(?:वादी|प्रतिवादी|आपत्तिकर्त्री|प्रार्थी)|मैं\s*शपथग्रहीता|मैं\s*शपथकर्ता)",
                ln,
            ) or ln.endswith(("प्रस्तुत हैं:", "प्रस्तुत है:", "निवेदन है कि:", "कथन है कि:", "बयान करता हूँ:", "बयान करती हूँ:")):
                opening_averment = (f"{opening_averment} {ln}".strip() if opening_averment else ln)
                continue
            title_lines.append(ln)
            continue

        if section == "pleadings":
            m_num = numbered_item_re.match(ln)
            if m_num and (m_num.group(1).startswith("यह कि") or len(m_num.group(1)) > 25):
                pleadings.append(m_num.group(1).strip())
            elif pleadings:
                pleadings[-1] = f"{pleadings[-1]} {ln}".strip()
            else:
                pleadings.append(ln)
            continue

        if section == "prayer":
            m_num = numbered_item_re.match(ln)
            if m_num:
                prayer.append(m_num.group(1).strip())
            elif re.match(r"^(?:\([क-ज्ञa-zA-Z0-9]+\))\s*(.+)$", ln):
                prayer.append(ln)
            elif prayer:
                prayer[-1] = f"{prayer[-1]} {ln}".strip()
            else:
                prayer.append(ln)
            continue

        if section == "verification":
            verification_lines.append(ln)
            continue

    # Deduplicate pleadings and prayer (in case of multi-page OCR overlap)
    dedup_pleadings: list[str] = []
    seen_p_keys: set[str] = set()
    for p in pleadings:
        p_clean = re.sub(r"^\s*(?:[*•▪▸]\s*|[0-9०-९]{1,3}\s*(?:\([^)]*\))?\s*[.):\-–—]+\s*)", "", p).strip()
        if not p_clean:
            continue
        k = _norm_dedup_key(p_clean)
        if k and len(k) >= 20 and k in seen_p_keys:
            continue
        if k:
            seen_p_keys.add(k)
        dedup_pleadings.append(p_clean)

    dedup_prayer: list[str] = []
    seen_pr_keys: set[str] = set()
    for pr in prayer:
        pr_clean = pr.strip()
        if not pr_clean:
            continue
        k = _norm_dedup_key(pr_clean)
        if k and len(k) >= 15 and k in seen_pr_keys:
            continue
        if k:
            seen_pr_keys.add(k)
        dedup_prayer.append(pr_clean)

    # Deduplicate signature_block lines while preserving order
    dedup_sig: list[str] = []
    for s_ln in signature_block:
        s_clean = s_ln.strip()
        if s_clean and s_clean not in dedup_sig:
            dedup_sig.append(s_clean)

    title_str = "\n".join(title_lines).strip()
    if not title_str:
        if document_type == "written_statement":
            title_str = "प्रतिवाद-पत्र / जवाब दावा"
        elif document_type == "application":
            title_str = "प्रार्थना-पत्र / आपत्ति-पत्र"
        elif document_type == "affidavit":
            title_str = "शपथ-पत्र (AFFIDAVIT)"
        elif document_type == "legal_notice":
            title_str = "विधिक नोटिस (LEGAL NOTICE)"
        else:
            title_str = "विधिक दस्तावेज़ / प्रार्थना-पत्र"

    return {
        "court_heading": court_heading,
        "case_heading": case_heading,
        "parties": parties,
        "title": title_str,
        "opening_averment": opening_averment,
        "pleadings": dedup_pleadings,
        "prayer": dedup_prayer,
        "verification": "\n".join(verification_lines).strip(),
        "signature_block": dedup_sig,
    }


def parse_single_page_document(raw_page_text: str, page_number: int = 1) -> dict[str, Any]:
    """Deterministically parse a single page (e.g., Page 1, Page 2, Page 3) of an existing legal case file
    so that recreating that single page preserves:
      - Exact footer page number (`single_page_number`, e.g., 3)
      - Exact starting paragraph number on that page (`start_paragraph_number`, e.g., 9)
      - Any unnumbered continuation lines at the top of the page from the previous page (`page_top_continuation`)
      - Zero injected fake titles (`"वाद पत्र"`) or fake signature blocks when they do not appear on that page.
    """
    text = str(raw_page_text or "").strip()
    if not text:
        return {}

    detected_page_no = max(1, int(page_number or 1))
    cleaned_lines: list[str] = []
    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        m_footer_pg = re.search(r"पृष्ठ\s*([0-9०-९]+)\s*(?:/\s*[0-9०-९]+)?", ln)
        if m_footer_pg:
            try:
                pg_val = int(m_footer_pg.group(1).translate(_DEVANAGARI_DIGITS_TRANS))
                if 1 <= pg_val <= 200 and page_number <= 1:
                    detected_page_no = pg_val
            except Exception:
                pass
            ln = re.sub(r"^.*?पृष्ठ\s*[0-9०-९]+(?:\s*/\s*[0-9०-९]+)?\s*$", "", ln).strip()
            if not ln:
                continue
        m_en_pg = re.match(r"^(?:page|p\.?)\s*[-–—:]?\s*(\d+)(?:\s*of\s*\d+)?$", ln, flags=re.I)
        if m_en_pg:
            if page_number <= 1:
                detected_page_no = max(1, int(m_en_pg.group(1)))
            continue
        if re.match(r"^[.\-_—–]{4,}$", ln):
            continue
        if re.match(r"^[-–—]?\s*([0-9०-९]{1,2})\s*[-–—]?$", ln):
            try:
                pg_val = int(re.sub(r"\D", "", ln.translate(_DEVANAGARI_DIGITS_TRANS)))
                if 1 <= pg_val <= 99 and page_number <= 1:
                    detected_page_no = pg_val
            except Exception:
                pass
            continue
        cleaned_lines.append(ln)

    # Split inline numbered "यह कि" points
    expanded_lines: list[str] = []
    for ln in cleaned_lines:
        parts = re.split(
            r"(?=(?:^|\s)(?:[*•▪▸]\s*|(?:प्रस्तर|धारा|पैरा)?\s*[0-9०-९]{1,3}\s*(?:\([^)]+\))?\s*[.):\-–—]\s*)यह\s+कि(?=\s))",
            ln,
        )
        for p in parts:
            if p.strip():
                expanded_lines.append(p.strip())

    # Detect whether this page actually has a Court Heading or Cause-Title (Page 1) or is a continuation page (Page 2, 3...)
    has_court_or_case_header = any(
        re.match(r"^(?:न्यायालय|माननीय\s+न्यायालय|समक्ष|सेवा\s+में)(?=\s|$)", ln)
        or re.search(r"(?:वाद\s*संख्या|प्रकीर्ण\s*वाद|मूल\s*वाद|मुकदमा\s*सं)", ln)
        or re.match(r"^बनाम\s*[:\-–—]*$", ln)
        for ln in expanded_lines[:8]
    )

    numbered_para_re = re.compile(
        r"^\s*(?:प्रस्तर|धारा|पैरा)?\s*([0-9०-९]{1,3})\s*(?:\([^)]*\))?\s*[.):\-–—]+\s*(.+)$"
    )
    yah_ki_unnumbered_re = re.compile(r"^\s*[*•▪▸\-]?\s*(यह\s+कि(?=\s|:).*)$")

    start_paragraph_number: int | None = None
    for ln in expanded_lines:
        m_np = numbered_para_re.match(ln)
        if m_np:
            body_cand = m_np.group(2).strip()
            # Ensure it's a pleading paragraph (not "1. राम कुमार ... वादी")
            if body_cand.startswith("यह कि") or not has_court_or_case_header or len(body_cand) > 45:
                try:
                    num_val = int(m_np.group(1).translate(_DEVANAGARI_DIGITS_TRANS))
                    if 1 <= num_val <= 200:
                        start_paragraph_number = num_val
                        break
                except Exception:
                    pass

    if start_paragraph_number is None:
        start_paragraph_number = 1

    # If this is a continuation page (no court/case header, or start_paragraph_number > 1), parse without inventing headers/titles
    if not has_court_or_case_header or start_paragraph_number > 1 or detected_page_no > 1:
        page_top_continuation_lines: list[str] = []
        pleadings: list[str] = []
        prayer: list[str] = []
        verification_lines: list[str] = []
        signature_block: list[str] = []
        section = "top"

        for ln in expanded_lines:
            if re.match(r"^[\s*•_\-–—]*(?:प्रार्थना|अनुतोष)(?:\s*\(Prayer\))?\s*[:\-–—]*\s*$", ln, flags=re.I):
                section = "prayer"
                continue
            m_prayer_inline = re.match(r"^[\s*•_\-–—]*(?:प्रार्थना|अनुतोष)\s*[:\-–—]+\s*(.+)$", ln)
            if m_prayer_inline:
                section = "prayer"
                prayer.append(m_prayer_inline.group(1).strip())
                continue
            if re.match(
                r"^(?:अतः|अतएव)\s+(?:माननीय\s+न्यायालय|श्रीमान\s+जी|न्यायालय)\s+से\s+(?:सादर\s+|सविनय\s+|विनम्र\s+)?प्रार्थना\s+है",
                ln,
            ):
                section = "prayer"
                prayer.append(ln)
                continue

            if re.match(r"^[\s*•_\-–—]*(?:सत्यापन|तस्दीक)(?:\s*\(Verification\))?\s*[:\-–—]*\s*$", ln, flags=re.I):
                section = "verification"
                continue
            m_ver_inline = re.match(r"^[\s*•_\-–—]*(?:सत्यापन|तस्दीक)(?:\s*\(Verification\))?\s*[:\-–—]+\s*(.+)$", ln, flags=re.I)
            if m_ver_inline:
                section = "verification"
                verification_lines.append(m_ver_inline.group(1).strip())
                continue
            if re.match(
                r"^(?:मैं|हम)\s*,?\s*(?:उपरोक्त|शपथग्रहीता|शपथग्रहीत्री|शपथकर्त्री|शपथकर्ता|वादी|प्रतिवादी|आपत्तिकर्त्री|प्रार्थी).*(?:सत्यापित|तस्दीक)\s+कर",
                ln,
            ):
                section = "verification"
                verification_lines.append(ln)
                continue

            if section in {"pleadings", "prayer", "verification", "signature"} and (
                re.match(
                    r"^(?:दिनांक|स्थान|तारीख|हस्ताक्षर|पहचानकर्ता|द्वारा\s*अधिवक्ता|अधिवक्ता|एडवोकेट|प्रार्थीगण|प्रार्थी|आवेदकगण|आपत्तिकर्त्री|आपत्तिकर्ता|वादीगण|वादी|प्रतिवादीगण|प्रतिवादी|शपथग्रहीता|शपथग्रहीत्री|शपथकर्त्री|शपथकर्ता|मो०|मोबाइल|पंजीकरण\s*सं)(?=\s|$|[:।.,;\-–—(])",
                    ln,
                )
                or (section == "signature" and len(ln) <= 65)
            ):
                section = "signature"
                signature_block.append(ln)
                continue

            if _PART2_HEADING_MATCH_RE.match(ln):
                section = "pleadings"
                pleadings.append(SECTION_DIVIDER_HEADING)
                continue

            m_np = numbered_para_re.match(ln)
            if m_np and section in {"top", "pleadings"}:
                section = "pleadings"
                pleadings.append(m_np.group(2).strip())
                continue

            m_yk = yah_ki_unnumbered_re.match(ln)
            if m_yk and section in {"top", "pleadings"}:
                section = "pleadings"
                pleadings.append(m_yk.group(1).strip())
                continue

            if section == "top":
                page_top_continuation_lines.append(ln)
            elif section == "pleadings":
                if pleadings and pleadings[-1] != SECTION_DIVIDER_HEADING:
                    pleadings[-1] = f"{pleadings[-1]} {ln}".strip()
                else:
                    pleadings.append(ln)
            elif section == "prayer":
                if re.match(r"^(?:\([क-ज्ञa-zA-Z0-9]+\))\s*(.+)$", ln):
                    prayer.append(ln)
                elif prayer:
                    prayer[-1] = f"{prayer[-1]} {ln}".strip()
                else:
                    prayer.append(ln)
            elif section == "verification":
                verification_lines.append(ln)

        return {
            "court_heading": "",
            "case_heading": "",
            "parties": [],
            "title": "",
            "opening_averment": "",
            "pleadings": pleadings,
            "prayer": prayer,
            "verification": "\n".join(verification_lines).strip(),
            "signature_block": signature_block,
            "layout": {
                "single_page_mode": True,
                "single_page_number": detected_page_no,
                "start_paragraph_number": start_paragraph_number,
                "page_top_continuation": " ".join(page_top_continuation_lines).strip(),
            },
        }

    # Otherwise, it is Page 1 (with headers/parties/title) — parse normally, but do not inject fake signature/verification if absent on Page 1
    full_parsed = parse_structured_legal_document(text)
    full_parsed["layout"] = {
        "single_page_mode": True,
        "single_page_number": detected_page_no,
        "start_paragraph_number": start_paragraph_number,
        "page_top_continuation": "",
    }
    return full_parsed


def fast_extract_as_is_state(state: CaseState, text: str) -> dict[str, Any]:
    """Deterministically extract case facts from raw text in `as_is_mode` or `single_page_mode` (or for long structured documents)
    without making slow multi-round LLM calls during intake.
    """
    all_user_msgs = state.user_messages(limit=40)
    combined_raw = "\n\n".join(all_user_msgs).strip() if all_user_msgs else str(text or "").strip()
    if state.style_mode == "single_page_mode" or getattr(state, "single_page_mode", False):
        parsed = parse_single_page_document(combined_raw, page_number=getattr(state, "target_page_number", 1) or 1)
    else:
        parsed = parse_structured_legal_document(combined_raw, document_type=state.document_type)

    extracted: dict[str, Any] = {
        "raw_source_text": combined_raw,
    }
    if parsed.get("court_heading"):
        extracted["court_name"] = parsed["court_heading"]
    if parsed.get("case_heading"):
        m_no = re.search(r"([0-9०-९]+)\s*/\s*([0-9०-९]{4})", parsed["case_heading"])
        if m_no:
            extracted["case_number"] = m_no.group(1).translate(_DEVANAGARI_DIGITS_TRANS)
            extracted["case_year"] = m_no.group(2).translate(_DEVANAGARI_DIGITS_TRANS)
    if parsed.get("parties"):
        extracted["parties"] = parsed["parties"]
        pls: list[str] = []
        dfs: list[str] = []
        for idx, p_line in enumerate(parsed["parties"]):
            clean_p = _strip_party_role_affixes(p_line)
            if not clean_p:
                continue
            if any(w in p_line for w in ("प्रतिवादी", "आपत्तिकर्त्री", "आपत्तिकर्ता", "विपक्षी", "उत्तरदाता")) or idx >= 1:
                dfs.append(clean_p)
            else:
                pls.append(clean_p)
        if pls:
            extracted["plaintiffs"] = pls
        if dfs:
            extracted["defendants"] = dfs
    if parsed.get("title"):
        extracted["purpose"] = parsed["title"].replace("\n", " — ")

    if parsed.get("pleadings"):
        extracted["facts"] = parsed["pleadings"]
    else:
        raw_lines = [ln.strip() for ln in combined_raw.splitlines() if ln.strip()]
        if raw_lines:
            extracted["facts"] = raw_lines

    if parsed.get("prayer"):
        extracted["reliefs"] = parsed["prayer"]

    for sig_ln in parsed.get("signature_block") or []:
        if "स्थान" in sig_ln:
            m_pl = re.sub(r"^.*?स्थान\s*[:\-–—]*\s*", "", sig_ln).strip()
            if m_pl and not m_pl.startswith("."):
                extracted["place"] = m_pl
        if "अधिवक्ता" in sig_ln or "एडवोकेट" in sig_ln or "शुक" in sig_ln:
            m_adv = re.sub(r"^.*?द्वारा\s*अधिवक्ता\s*[:\-–—]*\s*", "", sig_ln).strip()
            if m_adv:
                extracted["advocate_name"] = m_adv

    return extracted


_TOP_HEADING_DROP_RE = re.compile(
    r"^\s*(?:भाग\s*(?:प्रथम|एक|1)|"
    r"(?:वादपत्र\s*की\s*)?(?:धारावार|पैरावार|तथ्यवार)\s*(?:प्रतिपक्षी\s*)?(?:उत्तर|जवाब)|"
    r"प्रारम्भिक\s*(?:कथन|आपत्तियाँ|आपत्तियां)|"
    r"प्राथमिक\s*(?:आपत्तियाँ|आपत्तियां))\s*[:\-–—]*\s*$"
)

_PART2_HEADING_MATCH_RE = re.compile(
    r"^\s*(?:भाग\s*(?:द्वितीय|दो|2)\s*[-–—:]\s*)?"
    r"(?:अतिरिक्त\s*कथन(?:\s*एवं\s*विशेष\s*वैधानिक\s*आपत्तियाँ|\s*एवं\s*विशेष\s*वैधानिक\s*आपत्तियां|\s*\(\s*उजरात\s*मजीद\s*\))?|"
    r"विशेष\s*(?:वैधानिक\s*)?(?:कथन|आपत्तियाँ|आपत्तियां)|"
    r"उजरात\s*मजीद)\s*[:\-–—]*\s*$"
)

_INLINE_HEADING_PREFIX_RE = re.compile(
    r"^\s*(?:यह\s*कि\s*)?(?:भाग\s*(?:प्रथम|द्वितीय|1|2)\s*[-–—:]\s*)?"
    r"(?:(?:वादपत्र\s*की\s*)?(?:धारावार|पैरावार|तथ्यवार)\s*(?:/\s*(?:पैरावार|धारावार)\s*)?(?:प्रतिपक्षी\s*)?(?:उत्तर|जवाब)|"
    r"प्रारम्भिक\s*(?:कथन))\s*[:\-–—]+\s*"
)

_STATUTORY_CITATION_MARKERS = (
    "भारतीय साक्ष्य अधिनियम",
    "साक्ष्य अधिनियम",
    "विशिष्ट अनुतोष अधिनियम",
    "सिविल प्रक्रिया संहिता",
    "जाप्ता दीवानी",
    "जा०दी०",
    "सी०पी०सी०",
    "सी.पी.सी.",
    "सी पी सी",
    "c.p.c",
    "cpc",
    "भारतीय नागरिक सुरक्षा संहिता",
    "बी०एन०एस०एस०",
    "बी.एन.एस.एस.",
    "bnss",
    "भारतीय न्याय संहिता",
    "बी०एन०एस०",
    "bns",
    "भारतीय दंड संहिता",
    "भा०द०सं०",
    "ipc",
    "दंड प्रक्रिया संहिता",
    "द०प्र०सं०",
    "crpc",
    "उत्तर प्रदेश राजस्व संहिता",
    "उ०प्र० राजस्व संहिता",
    "राजस्व संहिता",
    "भारतीय परिसीमा अधिनियम",
    "परिसीमा अधिनियम",
    "उत्तर प्रदेश न्यायालय शुल्क अधिनियम",
    "न्यायालय शुल्क अधिनियम",
    "वाद मूल्यांकन अधिनियम",
    "सम्पत्ति अंतरण अधिनियम",
    "संपत्ति अंतरण अधिनियम",
    "आदेश 7 नियम",
    "आदेश 1 नियम",
    "आदेश 6 नियम",
    "आदेश 8 नियम",
    "आदेश 9 नियम",
    "आदेश 26 नियम",
    "आदेश 39 नियम",
    "धारा 35ए",
    "धारा 35-ए",
    "धारा 35अ",
    "दफा 35",
    "धारा 41(",
    "धारा 41 (",
    "धारा 34",
    "धारा 38",
    "धारा 39",
    "धारा 206",
    "धारा 115",
    "धारा 101",
    "धारा 104",
    "धारा 151",
)


def _strip_prohibited_statutes_from_pleading(text: str) -> str:
    """Remove prohibited statutory citations (CPC, BNSS, BNS, IPC, CrPC, Acts, Orders/Rules, Sections)
    from a pleading paragraph while preserving 'वादपत्र की धारा X' (which means Paragraph X of the Plaint)."""
    s = str(text or "").strip()
    if not s:
        return ""

    # 1. Remove leading "विशेष वैधानिक आपत्ति (अंतर्गत ...) :" prefixes
    s = re.sub(
        r"^(?:यह\s*कि\s*)?(?:क्षेत्राधिकार\s*संबंधी\s*)?विशेष\s*वैधानिक\s*आपत्ति\s*(?:\([^)]*\))?\s*[:\-–—]\s*",
        "यह कि ",
        s,
    )
    # 2. Remove parenthetical statutory citations like "(अंतर्गत धारा 34 ... अधिनियम, 1963)" or "(धारा 41(एच))" or "(BNSS)"
    s = re.sub(
        r"\(\s*(?:अंतर्गत\s*|सपठित\s*)?(?:धारा|दफा|आदेश|BNSS|BNS|CPC|IPC|CrPC|सी०पी०सी०|बी०एन०एस०एस०)[^)]*\)",
        "",
        s,
        flags=re.IGNORECASE,
    )
    # 3. Temporarily protect "वादपत्र की धारा", "वाद पत्र की धारा", "प्रतिवादपत्र की धारा", "शपथपत्र की धारा"
    protected_map: dict[str, str] = {}

    def _protect(m: re.Match) -> str:
        key = f"__PL_PARA_{len(protected_map)}__"
        protected_map[key] = m.group(0)
        return key

    s = re.sub(
        r"(?:वाद\s*पत्र|वादपत्र|प्रतिवाद\s*पत्र|प्रतिवादपत्र|शपथ\s*पत्र|शपथपत्र)\s*(?:की|के)\s*धारा\s*\d+(?:\s*\([^)]+\))?",
        _protect,
        s,
    )

    # 4. Strip inline statutory Act / Code / Section / Order-Rule phrases
    sec_num_pat = r"\d+(?:\s*\([^)]{1,20}\))?(?:\s*[-–—]\s*[A-Za-z\u0900-\u097F]{1,2}|[A-Za-zक-ज्ञअ-औ]{1,2}(?=\s|$|[,।.)]))?(?:\s*\([A-Za-z\u0900-\u097F0-9\-]{1,6}\))*"
    act_names_pat = (
        r"(?:भारतीय\s*नागरिक\s*सुरक्षा\s*संहिता|बी०एन०एस०एस०|बी\.एन\.एस\.एस\.|BNSS|"
        r"भारतीय\s*न्याय\s*संहिता|बी०एन०एस०|BNS|"
        r"सिविल\s*प्रक्रिया\s*संहिता|व्यवहार\s*प्रक्रिया\s*संहिता|सी०पी०सी०|सी\.पी\.सी\.|सी\s*पी\s*सी|CPC|जाप्ता\s*दीवानी|जा०दी०|"
        r"भारतीय\s*दंड\s*संहिता|भा०द०सं०|IPC|दंड\s*प्रक्रिया\s*संहिता|द०प्र०सं०|CrPC|"
        r"विशिष्ट\s*अनुतोष\s*अधिनियम|उत्तर\s*प्रदेश\s*राजस्व\s*संहिता|उ०प्र०\s*राजस्व\s*संहिता|राजस्व\s*संहिता|"
        r"भारतीय\s*साक्ष्य\s*अधिनियम|साक्ष्य\s*अधिनियम|भारतीय\s*परिसीमा\s*अधिनियम|परिसीमा\s*अधिनियम|"
        r"उत्तर\s*प्रदेश\s*न्यायालय\s*शुल्क\s*अधिनियम|न्यायालय\s*शुल्क\s*अधिनियम|वाद\s*मूल्यांकन\s*अधिनियम|"
        r"सम्पत्ति\s*अंतरण\s*अधिनियम|संपत्ति\s*अंतरण\s*अधिनियम)"
        r"(?:\s*,?\s*\d{4}(?:\s*\([^)]+\))?)?"
    )

    statute_patterns = [
        rf"(?:{act_names_pat}\s*(?:के|की)\s*)?(?:अंतर्गत\s*|अन्तर्गत\s*|के\s*अंतर्गत\s*|हस्व\s*)?आदेश\s*\d+(?:\s*\([^)]+\))?\s*(?:के\s*परंतुक\s*(?:एवं|व)\s*|व\s*)?(?:नियम\s*{sec_num_pat}(?:\s*(?:व|एवं|तथा)\s*{sec_num_pat})*)?(?:\s*(?:सहपठित|सपठित)\s*धारा\s*{sec_num_pat})?(?:\s*{act_names_pat})?(?:\s*(?:के\s*अंतर्गत|के\s*तहत|के\s*प्रावधानों?\s*से|से))?",
        rf"{act_names_pat}\s*(?:की|के)\s*(?:धारा|दफा|नियम)\s*{sec_num_pat}(?:\s*(?:व|एवं|तथा|,)\s*(?:धारा\s*)?{sec_num_pat})*(?:\s*(?:के\s*अंतर्गत|के\s*तहत|के\s*प्रावधानों?\s*से|से))?",
        rf"(?:अंतर्गत\s*|अन्तर्गत\s*|सपठित\s*|सहपठित\s*|हस्व\s*)?(?:धारा|दफा)\s*{sec_num_pat}(?:\s*(?:व|एवं|तथा|,)\s*(?:धारा\s*)?{sec_num_pat})*(?:\s*(?:सपठित|सहपठित)\s*धारा\s*{sec_num_pat})?(?:\s*{act_names_pat})?(?:\s*(?:के\s*अंतर्गत|के\s*तहत|के\s*प्रावधानों?\s*से|से))?",
        rf"{act_names_pat}(?:\s*(?:के\s*प्रावधानों?\s*से|के\s*अंतर्गत|के\s*तहत|से))?",
    ]
    for pat in statute_patterns:
        s = re.sub(pat, " ", s, flags=re.IGNORECASE)

    # Restore protected "वादपत्र की धारा ..." tokens
    for key, orig in protected_map.items():
        s = s.replace(key, orig)

    # Clean up residual connector / "बाधित है" artifacts left after removing statutory references
    s = re.sub(r"\s*(?:तथा|एवं|और)\s*(?:एवं\s*)?(?:पूर्णतः\s*)?(?:बाधित|वर्जित)\s*है", "", s)
    s = re.sub(r"\s+(?:के\s*अंतर्गत|के\s*तहत|सपठित|सहपठित)\s+", " ", s)
    s = re.sub(r"\s+(?:की|के|तथा|एवं|और)\s+(?=[,।.]|$)", "", s)
    s = re.sub(r"(?:^|\s)(?:अंतर्गत|अन्तर्गत|सपठित|सहपठित|हस्व)\s+", " ", s)
    s = re.sub(r"\s*,\s*,+", ", ", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;:-–—")

    if re.match(
        r"^(?:यह\s*कि\s*)?(?:दावा\s*वादी|वादी\s*का\s*वाद|वादीगण\s*का\s*वाद|प्रस्तुत\s*वाद)?\s*(?:के\s*प्रावधानों?\s*से\s*|विधि\s*द्वारा\s*|नियम\s*से\s*)?(?:पूर्णतः\s*)?(?:बाधित\s*है|वर्जित\s*है)[।.]?$",
        s,
    ):
        return ""

    if s and not s.startswith("यह कि") and not _PART2_HEADING_MATCH_RE.match(s):
        s = "यह कि " + s
    if s and not s.endswith(("।", ".", "!", "?")) and not _PART2_HEADING_MATCH_RE.match(s):
        s += "।"
    return s


def _structure_written_statement_two_parts(pleadings: list[str], facts: dict[str, Any]) -> tuple[list[str], int, int]:
    """Structure a Written Statement (जवाब दावा) into:
    1. Part 1: Pure 1-to-1 para-wise factual replies to the Vaad Patra ('वादपत्र की धारा 1 (एक)...') with ZERO statutory citations.
    2. Centered Section Heading: 'अतिरिक्त कथन (उजरात मजीद)'
    3. Part 2: Purely factual additional defense statements (अतिरिक्त कथन) with ZERO legal section / CPC / BNSS citations.
    Returns (structured_pleadings_list, part1_count, total_numbered_paragraphs).
    """
    part1_replies: list[str] = []
    part2_factual_defense: list[str] = []
    in_part2 = False

    for raw_p in pleadings:
        p = str(raw_p or "").strip()
        if not p:
            continue
        p = re.sub(r"\bvexatious\b", "मिथ्या एवं तंग करने वाले", p, flags=re.I)
        p = re.sub(r"\bfrivolous\b", "निराधार एवं तुच्छ", p, flags=re.I)
        p = re.sub(r"\bmaintainable\b", "पोषणीय", p, flags=re.I)

        p_no_num = re.sub(r"^\s*\d+\s*(?:\([^)]*\))?\s*[:.\-–—]+\s*", "", p).strip()

        if _TOP_HEADING_DROP_RE.match(p_no_num):
            continue
        if not p_no_num.startswith("यह कि") and len(p_no_num.split()) <= 10 and any(
            w in p_no_num for w in ("भाग प्रथम", "धारावार", "पैरावार", "तथ्यवार")
        ):
            continue

        if _PART2_HEADING_MATCH_RE.match(p_no_num):
            in_part2 = True
            continue

        p_body = _INLINE_HEADING_PREFIX_RE.sub("", p_no_num).strip()
        if not p_body:
            continue

        is_plaint_para_reply = bool(re.search(r"(?:वाद\s*पत्र|वादपत्र)\s*(?:की|के)\s*(?:धारा|पैरा)", p_body))

        cleaned_para = _strip_prohibited_statutes_from_pleading(p_body)
        if not cleaned_para:
            continue

        if not in_part2 and is_plaint_para_reply:
            part1_replies.append(cleaned_para)
        else:
            in_part2 = True
            part2_factual_defense.append(cleaned_para)

    if not any("वाद कारण" in x or "वादकारण" in x for x in part2_factual_defense):
        part2_factual_defense.insert(
            0,
            "यह कि वादी को प्रतिवादीगण के विरुद्ध कोई वाद कारण उत्पन्न नहीं हुआ है और न ही वादी माननीय न्यायालय से प्रतिवादीगण के विरुद्ध किसी भी अनुतोष को पाने का अधिकारी है।",
        )

    if not any("वास्तविक तथ्य" in x or "पेशबंदी" in x or "दरअसल" in x for x in part2_factual_defense):
        part2_factual_defense.append(
            "यह कि वास्तविक तथ्य यह है कि प्रतिवादीगण शांतिप्रिय एवं कानून का पालन करने वाले नागरिक हैं तथा अपनी स्वयं की वैध सम्पत्ति पर अधिकारपूर्वक शांतिपूर्ण रूप से काबिज व दखील चले आ रहे हैं। "
            "वादी ने वास्तविक तथ्यों को छिपाकर महज पेशबंदी के तौर पर एवं प्रतिवादीगण पर नाजायज दबाव बनाने की नीयत से यह मनगढ़ंत व निराधार मुकदमा दायर किया है।"
        )

    if not any("स्वच्छ" in x or "छिपाया" in x for x in part2_factual_defense):
        part2_factual_defense.append(
            "यह कि वादी ने विवादित सम्पत्ति एवं मौके की वास्तविक स्थिति के संबंध में महत्वपूर्ण तथ्यों को जानबूझकर छिपाया है तथा स्वच्छ व निर्मल हाथों से माननीय न्यायालय के समक्ष उपस्थित नहीं हुआ है, जिस कारण दावा वादी पोषणीय नहीं है।"
        )

    if not any("मूल्यांकन" in x or "न्याय शुल्क" in x for x in part2_factual_defense):
        part2_factual_defense.append(
            "यह कि वादी ने वाद का मूल्यांकन गलत एवं अत्यंत कम किया है तथा अदा किया गया न्याय शुल्क अपर्याप्त है।"
        )

    if not any("हर्जा" in x or "निरस्त होने योग्य" in x or "खारिज होने योग्य" in x for x in part2_factual_defense):
        part2_factual_defense.append(
            "यह कि उपरोक्त वर्णित तथ्यों एवं परिस्थितियों के आधार पर वादी का वाद पूर्णतः झूठे व आधारहीन कथनों पर आधारित होने के कारण हर हालत में विशेष हर्जा व खर्चा सहित निरस्त किए जाने योग्य है।"
        )

    structured = part1_replies + [SECTION_DIVIDER_HEADING] + part2_factual_defense
    part1_count = len(part1_replies)
    total_numbered = len(part1_replies) + len(part2_factual_defense)
    return structured, part1_count, total_numbered


def _is_court_suit_affidavit(facts: dict[str, Any]) -> bool:
    """Return True if an Affidavit belongs to an adversarial court case (Plaintiff vs Defendant),
    or False if it is an administrative, university, scholarship, revenue, or general notary affidavit."""
    if facts.get("case_number"):
        return True
    if facts.get("plaintiffs") and facts.get("defendants"):
        return True
    court = str(facts.get("court_name") or "").strip()
    if any(k in court for k in ("सिविल जज", "जिला जज", "अपर जिला", "न्यायालय श्रीमान")) and (facts.get("plaintiffs") or facts.get("defendants")):
        return True
    return False


def _normalize_affidavit_structure(out: dict[str, Any], facts: dict[str, Any], document_type: str = "affidavit") -> dict[str, Any]:
    """Enforce canonical Indian Affidavit (शपथ-पत्र) structure while preserving GPT-6.0-Luna's domain-specific sworn statements."""
    is_suit = _is_court_suit_affidavit(facts) or document_type == "evidence_pw_affidavit"
    party_name, _ = _extract_party_names_for_doc(facts, document_type)
    deponent_full = str(facts.get("deponent") or party_name or "शपथग्रहीता").strip()
    deponent_full = re.sub(r"^\s*मैं\s*,?\s*(?:शपथग्रहीता|शपथग्रहीत्री|शपथकर्त्री|शपथकर्ता)?\s*", "", deponent_full).strip()

    # 1. Authority / Forum Heading (`court_heading`)
    ch_forum = str(out.get("court_heading") or facts.get("court_name") or "").strip()
    if ch_forum:
        if not is_suit and not ch_forum.startswith(("समक्ष", "न्यायालय")):
            ch_forum = f"समक्ष : {ch_forum}"
        out["court_heading"] = ch_forum
    elif not is_suit:
        out["court_heading"] = "समक्ष : संबंधित सक्षम प्राधिकारी / नोटरी पब्लिक महोदय"

    # 2. Case Heading & Parties (`case_heading` & `parties`)
    if not is_suit:
        raw_ch = str(out.get("case_heading") or "").strip()
        if "मूल वाद" in raw_ch or "प्रकीर्ण वाद" in raw_ch or "बनाम" in raw_ch or "...." in raw_ch:
            out["case_heading"] = ""
        out["parties"] = []
    else:
        raw_ch = str(out.get("case_heading") or "").strip()
        case_no = str(facts.get("case_number") or "............").strip()
        case_yr = str(facts.get("case_year") or "2026").strip()
        if not raw_ch or ("बनाम" in raw_ch and "वाद" not in raw_ch):
            out["case_heading"] = f"मूल वाद संख्या {case_no} सन् {case_yr}"
        if out.get("parties"):
            out["parties"] = _format_cause_title_parties(out["parties"], facts)

    # 3. Title (`title`)
    title = str(out.get("title") or "").strip()
    if not title or any(w in title for w in ("लिखित कथन", "जवाब दावा", "प्रतिवाद पत्र", "वाद पत्र बाबत")):
        purpose_short = str(facts.get("purpose") or "").strip()
        if document_type == "evidence_pw_affidavit":
            out["title"] = "साक्ष्य शपथ-पत्र (बयान चीफ)"
        elif purpose_short and len(purpose_short) <= 75 and "प्रस्तुत करने हेतु" not in purpose_short:
            out["title"] = f"शपथ-पत्र ({purpose_short})"
        else:
            out["title"] = "शपथ-पत्र (AFFIDAVIT)"

    # 4. Deponent Introduction (`opening_averment`)
    opening = str(out.get("opening_averment") or "").strip()
    if not opening or "प्रतिवादीगण की ओर से" in opening or "वादी निम्न प्रकार" in opening:
        out["opening_averment"] = (
            f"मैं शपथग्रहीता / शपथकर्ता {deponent_full}, सत्यनिष्ठा से शपथपूर्वक निम्नलिखित बयान करता/करती हूँ :-"
        )

    # 5. Sworn Statements (`pleadings`) — merge any stray `prayer` items into the final paragraph of `pleadings`
    pleadings = [str(p).strip() for p in (out.get("pleadings") or []) if str(p).strip()]
    cleaned_pleadings: list[str] = []
    for p in pleadings:
        p_no_num = re.sub(r"^\s*(?:[*•▪▸]\s*|(?:प्रस्तर|धारा|पैरा)?\s*[0-9०-९]+\s*(?:\([^)]*\))?\s*[:.\-–—]+\s*)", "", p).strip()
        if not p_no_num:
            continue
        if not p_no_num.startswith("यह कि"):
            p_no_num = f"यह कि {p_no_num}"
        if not p_no_num.endswith(("।", ".", "!", "?")):
            p_no_num += "।"
        cleaned_pleadings.append(p_no_num)

    prayer_items = [str(x).strip() for x in (out.get("prayer") or []) if str(x).strip()]
    if prayer_items and document_type == "affidavit":
        for pr in prayer_items:
            pr_clean = re.sub(r"^\s*(?:\([क-ज्ञa-zA-Z0-9]+\)\s*)", "", pr).strip()
            if pr_clean and not any(pr_clean[:30] in existing for existing in cleaned_pleadings):
                if not pr_clean.startswith("यह कि"):
                    pr_clean = f"यह कि शपथग्रहीता विनम्रतापूर्वक निवेदन करता है कि {pr_clean}"
                if not pr_clean.endswith(("।", ".")):
                    pr_clean += "।"
                cleaned_pleadings.append(pr_clean)
        out["prayer"] = []

    out["pleadings"] = cleaned_pleadings

    # 6. Verification (`verification`) — strictly in the name of `उपरोक्त शपथग्रहीता / शपथकर्ता`
    total_p = max(1, len(cleaned_pleadings))
    place_str = str(facts.get("place") or facts.get("court_complex") or "............").strip()
    date_str = str(facts.get("date") or "............").strip()
    ver = str(out.get("verification") or "").strip()
    if ver:
        ver_lines = [
            ln.strip() for ln in ver.splitlines()
            if ln.strip() and not re.match(r"^[\s\-–—:*_]*सत्यापन(?:\s*\(Verification\))?[\s\-–—:*_]*$", ln.strip(), flags=re.I)
        ]
        ver = "\n".join(ver_lines).strip()

    needs_aff_ver = (
        not ver
        or "वादी" in ver
        or "प्रतिवादी" in ver
        or "वाद पत्र" in ver
        or "वादपत्र" in ver
        or (total_p > 1 and str(total_p) not in ver)
    )
    if needs_aff_ver:
        ver = (
            f"मैं, {party_name} (उपरोक्त शपथग्रहीता / शपथकर्ता), सत्यनिष्ठा से सत्यापित करता/करती हूँ कि इस शपथ-पत्र के प्रस्तर/धारा 1 से {total_p} तक में "
            f"दी गई समस्त जानकारी मेरे निजी ज्ञान एवं उपलब्ध अभिलेखों के अनुसार पूर्णतः सत्य व सही है। इसमें कोई भी तथ्य छिपाया नहीं गया है और न ही कोई तथ्य असत्य है। ईश्वर मेरी मदद करे।"
        )
    out["verification"] = ver

    # 7. Deponent Signature Block (`signature_block`)
    explicit_adv = str(facts.get("advocate_name") or os.getenv("DEFAULT_ADVOCATE_NAME", "")).strip()
    sig_lines = [
        f"स्थान : {place_str}",
        f"दिनांक : {date_str}",
        f"हस्ताक्षर शपथग्रहीता / शपथकर्ता ({party_name})",
    ]
    if explicit_adv:
        sig_lines.append(f"पहचानकर्ता / अधिवक्ता — {explicit_adv}")
    out["signature_block"] = sig_lines
    return out


def _normalize_draft_structure(
    draft: dict[str, Any],
    facts: dict[str, Any],
    document_type: str = "dava_plaint",
    style_mode: str = "template_mode",
) -> dict[str, Any]:
    """Unwrap nested model payloads and ensure complete court-grade headings, clauses, prayer, verification, and Hindi numbers."""
    if not isinstance(draft, dict):
        return {}
    for wrap_key in ("draft", "output", "result", "data", "response"):
        inner = draft.get(wrap_key)
        if isinstance(inner, dict) and (inner.get("pleadings") or inner.get("title")):
            draft = {**draft, **inner}
            break

    out = dict(draft)
    for list_key in ("parties", "pleadings", "prayer", "signature_block"):
        raw_val = out.get(list_key)
        if isinstance(raw_val, str):
            items = [line.strip() for line in raw_val.splitlines() if line.strip()]
        elif isinstance(raw_val, list):
            items = [_flatten_str_item(x) for x in raw_val if _flatten_str_item(x)]
        else:
            items = []
        out[list_key] = items

    for str_key in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
        val = out.get(str_key)
        if isinstance(val, list):
            out[str_key] = "\n".join(_flatten_str_item(x) for x in val if _flatten_str_item(x))
        elif isinstance(val, dict):
            out[str_key] = _flatten_str_item(val)
        else:
            out[str_key] = str(val or "").strip()

    # In "Single-Page Recreation & Correction" mode (`single_page_mode`), preserve the exact sections present on that single page
    # without inventing fake titles, fake court headings, or fake signature blocks!
    is_single_page = style_mode == "single_page_mode" or bool((out.get("layout") or {}).get("single_page_mode"))
    if is_single_page:
        cleaned_sp_pleadings: list[str] = []
        for p in out.get("pleadings") or []:
            p_no_num = re.sub(
                r"^\s*(?:[*•▪▸]\s*|(?:प्रस्तर|धारा|पैरा)?\s*[0-9०-९]{1,3}\s*(?:\([^)]*\))?\s*[:.\-–—]+\s*)",
                "",
                str(p).strip(),
            ).strip()
            if p_no_num:
                cleaned_sp_pleadings.append(p_no_num)
        out["pleadings"] = cleaned_sp_pleadings
        if out.get("parties") and document_type not in {"legal_notice", "affidavit"}:
            out["parties"] = _format_cause_title_parties(out["parties"], facts)
        if out.get("verification"):
            ver_lines = [
                ln.strip() for ln in str(out["verification"]).splitlines()
                if ln.strip() and not re.match(r"^[\s\-–—:*_]*सत्यापन[\s\-–—:*_]*$", ln.strip())
            ]
            out["verification"] = "\n".join(ver_lines).strip()
        layout_meta = dict(out.get("layout") or {})
        layout_meta["single_page_mode"] = True
        out["layout"] = layout_meta
        return format_draft_numbers(out)

    # In "As-It-Is / Handwritten to Legal Format" mode (`as_is_mode`), preserve 100% of the advocate's exact document
    # structure, paragraphs, and wording without stripping any of their text or injecting boilerplate paragraphs.
    if style_mode == "as_is_mode":
        cleaned_as_is_pleadings: list[str] = []
        for p in out.get("pleadings") or []:
            p_no_num = re.sub(
                r"^\s*(?:[*•▪▸]\s*|(?:प्रस्तर|धारा|पैरा)?\s*[0-9०-९]{1,3}\s*(?:\([^)]*\))?\s*[:.\-–—]+\s*)",
                "",
                str(p).strip(),
            ).strip()
            if p_no_num:
                cleaned_as_is_pleadings.append(p_no_num)
        out["pleadings"] = cleaned_as_is_pleadings

        if not out.get("title"):
            out["title"] = str(facts.get("purpose") or "प्रार्थना-पत्र / आपत्ति-पत्र").strip()

        if out.get("parties") and document_type not in {"legal_notice", "affidavit"}:
            out["parties"] = _format_cause_title_parties(out["parties"], facts)

        if out.get("verification"):
            ver_lines = [
                ln.strip() for ln in str(out["verification"]).splitlines()
                if ln.strip() and not re.match(r"^[\s\-–—:*_]*सत्यापन[\s\-–—:*_]*$", ln.strip())
            ]
            out["verification"] = "\n".join(ver_lines).strip()

        if not out.get("signature_block"):
            place_str = str(facts.get("place") or facts.get("court_complex") or "............").strip()
            adv_str = str(facts.get("advocate_name") or "अधिवक्ता").strip()
            party_name, _ = _extract_party_names_for_doc(facts, document_type)
            out["signature_block"] = [
                f"स्थान : {place_str}",
                "दिनांक : ............",
                f"प्रार्थी / पक्षकार ({party_name})",
                f"द्वारा अधिवक्ता : {adv_str}",
            ]
        return format_draft_numbers(out)

    # Dedicated structural normalizer for all Affidavits (Judicial, University/Scholarship, Administrative, or Notary)
    if document_type in {"affidavit", "evidence_pw_affidavit"}:
        out = _normalize_affidavit_structure(out, facts, document_type=document_type)
        return format_draft_numbers(out)

    if not out.get("court_heading") and facts.get("court_name"):
        out["court_heading"] = str(facts["court_name"]).strip()

    # Fix case_heading: never allow a duplicate party summary ("... बनाम ...") in case_heading!
    ch = out.get("case_heading", "").strip()
    case_no = str(facts.get("case_number") or "............").strip()
    case_yr = str(facts.get("case_year") or "2026").strip()
    if document_type != "legal_notice":
        if not ch or ("बनाम" in ch and not re.search(r"(?:मूल\s*वाद|वाद\s*(?:सं|नंबर|न०)|प्रकीर्ण\s*वाद|कैवियट\s*सं|अपील\s*सं)", ch)):
            out["case_heading"] = f"मूल वाद संख्या {case_no} सन् {case_yr}"

    if not out.get("parties"):
        fallback_parties: list[str] = []
        for idx, pl in enumerate(facts.get("plaintiffs") or [], 1):
            s = _strip_party_role_affixes(str(pl))
            if s:
                fallback_parties.append(f"{s} — वादी")
        for idx, df in enumerate(facts.get("defendants") or [], 1):
            s = _strip_party_role_affixes(str(df))
            if s:
                fallback_parties.append(f"{s} — प्रतिवादी")
        if not fallback_parties and facts.get("parties"):
            fallback_parties = [str(x).strip() for x in facts["parties"] if str(x).strip()]
        if fallback_parties:
            out["parties"] = fallback_parties
    if out.get("parties") and document_type != "legal_notice":
        out["parties"] = _format_cause_title_parties(out["parties"], facts)

    if document_type in {"written_statement", "dava_plaint"} and out.get("title"):
        t_clean = re.sub(r"\s*,?\s*(?:अंतर्गत|के\s*अंतर्गत|तहत)\s+(?:आदेश|धारा|दफा)\s+.*$", "", out["title"]).strip()
        if document_type == "written_statement":
            out["title"] = t_clean or "प्रतिवादीगण की ओर से वादपत्र का लिखित कथन / जवाब दावा"
        else:
            out["title"] = t_clean or "वाद पत्र"

    # Strip any prohibited statutory citations (CPC, BNSS, BNS, IPC, Acts, Sections) from Dava Plaint pleadings
    # and ensure mandatory Jurisdiction & Valuation/Court-Fee paragraphs
    if document_type == "dava_plaint" and out.get("pleadings"):
        pleadings = [
            _strip_prohibited_statutes_from_pleading(p)
            for p in out["pleadings"]
            if _strip_prohibited_statutes_from_pleading(p)
        ]
        joined_pleadings = "\n".join(pleadings)

        has_jurisdiction = any(k in joined_pleadings for k in ("क्षेत्राधिकार", "कार्य क्षेत्र", "अधिकारिता", "वादश्रवण"))
        if not has_jurisdiction:
            prop_loc = str(facts.get("court_complex") or facts.get("place") or "न्यायालय के स्थानीय क्षेत्राधिकार").strip()
            pleadings.append(
                f"यह कि यह मान्य न्यायालय प्रस्तुत वाद की सुनवाई एवं निर्णय करने हेतु पूर्ण कार्य क्षेत्र व क्षेत्राधिकार रखती है क्योंकि वादग्रस्त सम्पत्ति {prop_loc} की सीमा के अंतर्गत स्थित है और प्रतिवादीगण भी इसी माननीय न्यायालय के क्षेत्राधिकार के भीतर निवास करते हैं।"
            )

        has_valuation = any(k in joined_pleadings for k in ("मूल्यांकन", "न्याय शुल्क", "न्यायालय शुल्क", "बाजारी कीमत", "मालियत"))
        if not has_valuation:
            val_amt = str(facts.get("valuation") or "............").strip()
            fee_str = str(facts.get("court_fee") or "नियमानुसार उचित न्याय शुल्क पृथक से अदा की गई है").strip()
            pleadings.append(
                f"यह कि वाद का मूल्यांकन स्थायी निषेधाज्ञा एवं वांछित अनुतोष हेतु रु० {val_amt} निश्चित है और नियमानुसार {fee_str}।"
            )

        out["pleadings"] = pleadings

    ws_part1_count = 0
    ws_total_numbered = len(out.get("pleadings") or [])
    if document_type == "written_statement" and out.get("pleadings"):
        out["pleadings"], ws_part1_count, ws_total_numbered = _structure_written_statement_two_parts(
            list(out["pleadings"]), facts
        )

    # Clean Prayer clauses: strip redundant lead-ins inside individual sub-clauses
    if out.get("prayer"):
        cleaned_prayer: list[str] = []
        for item in out["prayer"]:
            s = re.sub(
                r"^\s*(?:\([क-ज्ञa-zA-Z0-9]+\)\s*)?(?:अतः\s*)?(?:अत्यन्त\s*विनयपूर्वक\s*)?(?:माननीय\s*न्यायालय\s*से\s*|श्रीमान\s*जी\s*से\s*)?(?:सादर\s*|सविनय\s*)?प्रार्थना\s*(?:की\s*जाती\s*)?है\s*कि\s*[:-]*\s*",
                "",
                str(item).strip(),
            ).strip()
            if s:
                cleaned_prayer.append(s)
        out["prayer"] = cleaned_prayer

    # Clean Verification: remove duplicate leading "सत्यापन" heading line and ensure correct party role & paragraph ranges
    ver = str(out.get("verification") or "").strip()
    party_name, role_label = _extract_party_names_for_doc(facts, document_type)
    place_str = str(facts.get("court_complex") or facts.get("place") or "बमुकाम कचहरी").strip()
    total_p = ws_total_numbered if document_type == "written_statement" else len(out.get("pleadings") or [])

    if ver:
        ver_lines = [
            ln.strip() for ln in ver.splitlines()
            if ln.strip() and not re.match(r"^[\s\-–—:*_]*सत्यापन[\s\-–—:*_]*$", ln.strip())
        ]
        ver = "\n".join(ver_lines).strip()

        pl_short = ""
        if facts.get("plaintiffs"):
            pl_list = facts["plaintiffs"] if isinstance(facts["plaintiffs"], list) else [facts["plaintiffs"]]
            if pl_list:
                pl_short = _clean_short_name(pl_list[0])
        ws_needs_sync = (
            document_type == "written_statement"
            and (
                "उपरोक्त वादी" in ver
                or "वाद पत्र के पैरा" in ver
                or "वादपत्र के पैरा" in ver
                or (pl_short and pl_short in ver)
                or f"से {total_p}" not in ver
            )
        )
        if ws_needs_sync or (total_p >= 4 and "पैरा" not in ver and "धारा" not in ver):
            factual_end = (ws_part1_count + 1) if (document_type == "written_statement" and ws_part1_count > 0) else max(1, total_p - 2)
            factual_end = min(factual_end, max(1, total_p - 1))
            legal_start = min(total_p, factual_end + 1)
            doc_label = "प्रतिवाद पत्र / जवाब दावा (लिखित कथन)" if document_type == "written_statement" else "वाद पत्र"
            verb = "सत्यापित करते हैं" if "गण" in role_label else "सत्यापित करता हूँ"
            pronoun = "हम" if "गण" in role_label else "मैं"
            my_word = "हमारी" if "गण" in role_label else "मेरी"
            our_word = "हमारे" if "गण" in role_label else "मेरे"
            ver = (
                f"{pronoun} कि {party_name}, {role_label}, आज दिनांक ............ को स्थान {place_str} पर यह {verb} कि "
                f"{doc_label} की धारा 1 से {factual_end} तक के कथन {my_word} निजी जानकारी में सच व सही हैं तथा धारा {legal_start} से {total_p} तक के कथन "
                f"जानकारी एवं विश्वास के आधार पर {our_word} ज्ञान में सत्य हैं। कोई भी तथ्य असत्य नहीं है और न ही कोई तथ्य छिपाया गया है।"
            )
        out["verification"] = ver

    explicit_adv = str(facts.get("advocate_name") or os.getenv("DEFAULT_ADVOCATE_NAME", "")).strip()
    if not out.get("signature_block"):
        sig_role = "प्रतिवादीगण / उत्तरदातागण" if document_type == "written_statement" else "वादी"
        adv_line = f"द्वारा अधिवक्ता — {explicit_adv}" if explicit_adv else "द्वारा अधिवक्ता"
        out["signature_block"] = [
            f"स्थान : {place_str}",
            f"दिनांक : ............",
            f"{sig_role} — {party_name}",
            adv_line,
        ]
    elif not explicit_adv:
        cleaned_sig = []
        for s_line in out["signature_block"]:
            s_str = str(s_line).strip()
            if re.search(r"शुक्ला|shukla", s_str, flags=re.IGNORECASE):
                if "द्वारा" in s_str and not any("द्वारा अधिवक्ता" in x for x in cleaned_sig):
                    cleaned_sig.append("द्वारा अधिवक्ता")
                continue
            cleaned_sig.append(s_str)
        if cleaned_sig:
            out["signature_block"] = cleaned_sig

    return format_draft_numbers(out)


def _has_identifiable_party(draft: dict[str, Any], facts: dict[str, Any]) -> bool:
    source_names: list[str] = []
    for k in ("plaintiffs", "defendants", "parties", "sender", "recipient", "deponent"):
        v = facts.get(k, [])
        if isinstance(v, list):
            source_names.extend(str(x).strip() for x in v if str(x).strip())
        elif v and str(v).strip():
            source_names.append(str(v).strip())

    if not source_names:
        return True

    sanitized_sources = [str(sanitize_draft_unicode(s)) for s in source_names]
    text = json.dumps(draft, ensure_ascii=False).casefold()

    if any(s.casefold() in text for s in sanitized_sources if s):
        return True

    token_re = re.compile(r"[A-Za-z\u0900-\u097F]{2,}", re.UNICODE)
    name_tokens: list[str] = []
    for s in sanitized_sources:
        for tok in token_re.findall(s):
            low = tok.casefold()
            if len(low) >= 2 and low not in _PARTY_STOPWORDS:
                name_tokens.append(low)

    if any(tok in text for tok in name_tokens):
        return True

    all_sources_ascii = all(not any("\u0900" <= c <= "\u097f" for c in s) for s in sanitized_sources)
    draft_hindi_chars = sum("\u0900" <= c <= "\u097f" for c in text)
    if all_sources_ascii and draft_hindi_chars >= 80 and (draft.get("parties") or draft.get("signature_block")):
        return True

    return False


class MultiDraftOrchestrator:
    def __init__(self):
        self.client = GeminiClient()
        self.retriever = HybridCorpusRetriever()

    def collect(self, state: CaseState, new_facts: dict[str, Any]):
        state.merge_facts(new_facts)
        user_msgs = state.user_messages(limit=40)
        if user_msgs:
            # Always keep raw_source_text synchronized with all user messages in the session
            state.facts["raw_source_text"] = "\n\n".join(user_msgs)

        if state.document_type in {"affidavit", "evidence_pw_affidavit"} and not state.facts.get("deponent"):
            for fallback_key in ("plaintiffs", "parties", "sender"):
                vals = state.facts.get(fallback_key, [])
                if isinstance(vals, list) and vals:
                    state.facts["deponent"] = vals[0]
                    break
                elif isinstance(vals, str) and vals.strip():
                    state.facts["deponent"] = vals.strip()
                    break
            if not state.facts.get("deponent") and (state.facts.get("facts") or state.facts.get("raw_source_text")):
                state.facts["deponent"] = "शपथग्रहीता / शपथकर्ता"
        if state.document_type == "affidavit" and not state.facts.get("purpose") and (state.facts.get("facts") or state.facts.get("raw_source_text")):
            state.facts["purpose"] = "प्रस्तुत प्रकरण / विषय के संबंध में शपथपूर्वक कथन प्रस्तुत करने हेतु"

        missing = missing_fields(state.facts, state.document_type, style_mode=state.style_mode)
        if missing:
            state.status = "collecting"
            return {
                "status": "needs_information",
                "missing": missing,
                "questions": next_questions(state.facts, state.document_type, style_mode=state.style_mode),
            }
        state.status = "ready"
        return {"status": "ready", "facts": state.facts}

    def extract_and_collect(self, state: CaseState, text: str):
        # Fast-path for As-It-Is mode, Single-Page mode, or already-structured long pleadings (3+ 'यह कि' paragraphs)
        yah_ki_count = len(re.findall(r"(?:^|\n|\s)यह\s+कि(?=\s|:)", str(text or "")))
        if state.style_mode in {"as_is_mode", "single_page_mode"} or getattr(state, "single_page_mode", False) or yah_ki_count >= 3:
            fast_extracted = fast_extract_as_is_state(state, text)
            if fast_extracted.get("facts"):
                return self.collect(state, fast_extracted)

        extracted = extract_case_facts(
            text,
            current_facts=state.facts,
            document_type=state.document_type,
            user_history=state.user_messages(limit=int(os.getenv("GEMINI_INTAKE_HISTORY_MESSAGES", "40"))),
        )
        return self.collect(state, extracted)

    def refresh_intake(self, state: CaseState):
        if state.style_mode in {"as_is_mode", "single_page_mode"} or getattr(state, "single_page_mode", False):
            raw_text = str(state.facts.get("raw_source_text") or "\n\n".join(state.user_messages(limit=40))).strip()
            return self.collect(state, fast_extract_as_is_state(state, raw_text))
        refreshed = reconcile_case_state(
            state.facts,
            state.document_type,
            state.user_messages(limit=int(os.getenv("GEMINI_INTAKE_HISTORY_MESSAGES", "40"))),
        )
        return self.collect(state, refreshed)

    def _retrieve(self, facts: dict[str, Any], document_type: str, style_mode: str = "template_mode") -> tuple[str, str]:
        pieces = []
        for key in ("court_name", "plaintiff_intro", "defendant_intro", "property_description", "cause_of_action", "jurisdiction_facts", "purpose", "sender", "recipient", "deponent"):
            if facts.get(key):
                pieces.append(str(facts[key]))
        for key in ("facts", "reliefs", "demands", "defence_points", "documents", "custom_add_points"):
            val = facts.get(key, [])
            pieces.extend(str(x) for x in (val if isinstance(val, list) else [val]))
        query = " ".join(pieces)
        corpus_type = TYPE_META[document_type][0]
        return self.retriever.retrieve_for_style(query, document_type=corpus_type, style_mode=style_mode)

    def draft_live(self, state: CaseState):
        style_mode = (state.style_mode or "template_mode").strip().lower()
        if getattr(state, "single_page_mode", False):
            style_mode = "single_page_mode"
        if missing_fields(state.facts, state.document_type, style_mode=style_mode):
            raise ValueError("Required facts are still missing.")

        corpus_type, prompt_file, label = TYPE_META[state.document_type]

        # Special Mode 1: Single-Page Recreation & Correction Mode (`single_page_mode`)
        if style_mode == "single_page_mode":
            target_page_no = max(1, int(getattr(state, "target_page_number", 0) or state.facts.get("target_page_number") or 1))
            state.selected_template_name = f"Single-Page Recreation Blueprint (पृष्ठ {target_page_no})"

            raw_pages = state.facts.get("raw_source_pages")
            if isinstance(raw_pages, list) and len(raw_pages) >= target_page_no:
                raw_page_text = str(raw_pages[target_page_no - 1] or "").strip()
            else:
                raw_page_text = str(
                    state.facts.get("raw_source_text")
                    or "\n\n".join(state.user_messages(limit=40))
                    or "\n".join(str(x) for x in (state.facts.get("facts") or []))
                ).strip()

            parsed_page = parse_single_page_document(raw_page_text, page_number=target_page_no)
            correction_inst = str(
                getattr(state, "page_correction_instruction", "")
                or state.facts.get("page_correction_instruction")
                or ""
            ).strip()

            # Apply any inline correction instruction using DraftEditor first (deterministic & scope-aware)
            if correction_inst:
                try:
                    from .draft_editor import DraftEditor
                    ed = DraftEditor()
                    edit_payload = ed.parse(parsed_page, correction_inst)
                    parsed_page, _ = ed.apply(parsed_page, edit_payload)
                except Exception:
                    pass

            if not self.client.has_active_key():
                normalized_sp = _normalize_draft_structure(
                    parsed_page, state.facts, document_type=state.document_type, style_mode="single_page_mode"
                )
                draft = format_draft_numbers(sanitize_draft_unicode(normalized_sp))
                state.status = "ready"
                return draft

            layout_info = dict(parsed_page.get("layout") or {})
            sp_package: dict[str, Any] = {
                "DOCUMENT_TYPE": label,
                "TARGET_PAGE_NUMBER": layout_info.get("single_page_number", target_page_no),
                "START_PARAGRAPH_NUMBER": layout_info.get("start_paragraph_number", 1),
                "PAGE_TOP_CONTINUATION": layout_info.get("page_top_continuation", ""),
                "TARGET_PAGE_RAW_TEXT": raw_page_text,
                "STRUCTURAL_PAGE_PARSE": parsed_page,
                "CORRECTION_INSTRUCTION": correction_inst or "No changes requested — recreate this exact page cleanly in Legal format.",
            }
            try:
                raw_draft = self.client.generate_json(
                    system=SINGLE_PAGE_SPECIALIST_PROMPT,
                    prompt=json.dumps(sp_package, ensure_ascii=False, indent=2),
                    schema=SCHEMA,
                    thinking_level=os.getenv("GEMINI_DRAFTING_THINKING_LEVEL", "high"),
                    max_output_tokens=12288,
                )
                if len(parsed_page.get("pleadings") or []) > len((raw_draft or {}).get("pleadings") or []):
                    raw_draft["pleadings"] = parsed_page["pleadings"]
                raw_draft["layout"] = layout_info
                # Never allow LLM to inject fake title or fake court_heading if parsed_page didn't have them
                for k_empty in ("court_heading", "case_heading", "title", "opening_averment", "verification"):
                    if not parsed_page.get(k_empty):
                        raw_draft[k_empty] = ""
                for k_list_empty in ("parties", "prayer", "signature_block"):
                    if not parsed_page.get(k_list_empty):
                        raw_draft[k_list_empty] = []
            except Exception:
                raw_draft = parsed_page

            normalized_sp = _normalize_draft_structure(
                raw_draft, state.facts, document_type=state.document_type, style_mode="single_page_mode"
            )
            draft = format_draft_numbers(sanitize_draft_unicode(normalized_sp))
            state.status = "ready"
            return draft

        # Special Mode 2: "As-It-Is / Handwritten to Legal Structure" (`as_is_mode`)
        if style_mode == "as_is_mode":
            state.selected_template_name = "As-It-Is / Handwritten Legal Structure Blueprint"
            raw_source_text = str(
                state.facts.get("raw_source_text")
                or "\n\n".join(state.user_messages(limit=40))
                or "\n".join(str(x) for x in (state.facts.get("facts") or []))
            ).strip()

            parsed_doc = parse_structured_legal_document(raw_source_text, document_type=state.document_type)
            custom_adds = [str(x).strip() for x in (state.facts.get("custom_add_points") or []) if str(x).strip()]
            for pt in custom_adds:
                pt_clean = pt if pt.startswith("यह कि") else f"यह कि {pt}"
                if not pt_clean.endswith(("।", ".")):
                    pt_clean += "।"
                parsed_doc.setdefault("pleadings", []).append(pt_clean)

            # If running in an offline test environment without API keys and parsed_doc has 2+ paragraphs, use parsed_doc directly
            if not self.client.has_active_key() and len(parsed_doc.get("pleadings") or []) >= 2:
                normalized_raw = _normalize_draft_structure(
                    parsed_doc, state.facts, document_type=state.document_type, style_mode="as_is_mode"
                )
                draft = format_draft_numbers(sanitize_draft_unicode(normalized_raw))
                state.status = "ready"
                return draft

            # Let Microsoft Foundry GPT-6.0-Luna do the heavy lifting with Document Intelligence structural pre-parse
            as_is_rules = [
                "AS-IT-IS & HANDWRITTEN LEGAL STRUCTURING MODE: Do NOT replace the advocate's text with a corpus template.",
                "If RAW_SOURCE_DOCUMENT_TEXT is an existing A4/typed document (Dava, Affidavit, Legal Notice, Written Statement, Application, Objection Petition, etc.), preserve 100% of its wording, paragraphs, sequence, headings, party details, prayer, and verification AS-IT-IS so it renders onto Legal-size paper (`8.5\" × 14\"`).",
                "If RAW_SOURCE_DOCUMENT_TEXT is from a handwritten draft (हस्तलिखित ड्राफ्ट), fix any OCR character/matra recognition noise and organize the advocate's exact handwritten facts and paragraphs into proper Indian court legal structure (`court_heading`, `case_heading`, `parties`, `title`, `opening_averment`, numbered `pleadings` starting with 'यह कि ...', `prayer`, `verification`, `signature_block`).",
                "Write numerical values in bilingual format (digits + Hindi words in parentheses) and do NOT prepend paragraph numbers in `pleadings` (the renderer numbers them automatically).",
            ]
            package: dict[str, Any] = {
                "DOCUMENT_TYPE": label,
                "SELECTED_STYLE_MODE": STYLE_NAMES.get("as_is_mode"),
                "RAW_SOURCE_DOCUMENT_TEXT": raw_source_text,
                "DOCUMENT_INTELLIGENCE_STRUCTURAL_PARSE": parsed_doc,
                "EXTRACTED_CASE_FACTS": {k: v for k, v in state.facts.items() if k not in {"raw_source_text", "raw_source_pages"}},
                "SYSTEM_RULES": as_is_rules,
                "OUTPUT_REQUIREMENTS": {
                    "court_heading": "Court or Authority heading from the source document (or proper court heading for handwritten notes).",
                    "case_heading": "Case/Suit number line if applicable (leave '' for non-court affidavits or legal notices).",
                    "parties": "Party lines in proper cause-title structure (or sender/recipient for legal notices).",
                    "title": "Formal underlined document title from the source document (or proper legal title for handwritten notes).",
                    "opening_averment": "Opening line or deponent oath introduction.",
                    "pleadings": "All paragraphs/points from the source document in exact order (without leading numbers).",
                    "prayer": "Prayer clauses from the source document (or [] for affidavits).",
                    "verification": "Verification clause without repeating the heading word 'सत्यापन'.",
                    "signature_block": "Place, date, party/deponent signature, and advocate block.",
                },
            }
            system = AS_IS_SPECIALIST_PROMPT + "\n\n" + "\n".join(f"- {r}" for r in as_is_rules)
            try:
                raw_draft = self.client.generate_json(
                    system=system,
                    prompt=json.dumps(package, ensure_ascii=False, indent=2),
                    schema=SCHEMA,
                    thinking_level=os.getenv("GEMINI_DRAFTING_THINKING_LEVEL", "high"),
                    max_output_tokens=16384,
                )
                # Ensure zero paragraph loss on long documents (e.g. 17+ paragraphs):
                parsed_p = parsed_doc.get("pleadings") or []
                luna_p = (raw_draft or {}).get("pleadings") or []
                if len(parsed_p) >= 2 and len(luna_p) < len(parsed_p):
                    raw_draft["pleadings"] = parsed_p
                for k_field in ("court_heading", "case_heading", "parties", "title", "opening_averment", "prayer", "verification", "signature_block"):
                    if not raw_draft.get(k_field) and parsed_doc.get(k_field):
                        raw_draft[k_field] = parsed_doc[k_field]
            except Exception:
                raw_draft = parsed_doc if parsed_doc.get("pleadings") else {
                    "court_heading": str(state.facts.get("court_name") or ""),
                    "case_heading": "",
                    "parties": list(state.facts.get("parties") or []),
                    "title": str(state.facts.get("purpose") or label),
                    "opening_averment": "",
                    "pleadings": list(state.facts.get("facts") or [raw_source_text]),
                    "prayer": list(state.facts.get("reliefs") or []),
                    "verification": "",
                    "signature_block": [],
                }
            normalized_raw = _normalize_draft_structure(
                raw_draft, state.facts, document_type=state.document_type, style_mode="as_is_mode"
            )
            draft = format_draft_numbers(sanitize_draft_unicode(normalized_raw))
            state.status = "ready"
            return draft

        style = (PROMPTS / prompt_file).read_text(encoding="utf-8")
        context, selected_tpl = self._retrieve(state.facts, state.document_type, style_mode=style_mode)
        state.selected_template_name = selected_tpl
        style_anchor = (ROOT / "style_reference.txt").read_text(encoding="utf-8")

        prior_plaint_paragraphs: list[str] = []
        for doc_entry in reversed(state.documents or []):
            if doc_entry.get("document_type") == "dava_plaint" and isinstance(doc_entry.get("draft"), dict):
                raw_pl = doc_entry["draft"].get("pleadings") or []
                prior_plaint_paragraphs = [f"वादपत्र की धारा {i}: {p}" for i, p in enumerate(raw_pl, 1)]
                break

        if state.document_type == "affidavit":
            is_suit_aff = _is_court_suit_affidavit(state.facts)
            context_to_send = context if is_suit_aff else (
                "UNIVERSAL AFFIDAVIT MODE (GPT-6.0-LUNA CONTENT + BOT CANONICAL STRUCTURE):\n"
                "Draft the substantive Hindi affidavit clauses directly from CASE_FACTS for this exact purpose/authority "
                "(e.g., University/Scholarship/Administrative/Departmental/Notary/Court Affidavit). Do NOT copy unrelated land/property disputes."
            )
            mode_rules = [
                "UNIVERSAL AFFIDAVIT ENGINE: Let GPT-6.0-Luna craft the exact Hindi sworn statements (`pleadings`) for ANY type of affidavit (University, Scholarship, OTR unblock, Samaj Kalyan Vibhag, Tehsil, SDM, Bank, Name/DOB correction, Gap Year, or Civil Court Affidavit) while strictly adhering to the bot's canonical Affidavit structure.",
                "In `court_heading`, place the exact Authority or Court (`समक्ष : ...` or `न्यायालय श्रीमान ...`).",
                "If this is an Administrative / University / Scholarship / Notary Affidavit (not a lawsuit between वादी and प्रतिवादी), leave `case_heading` as '' and `parties` as [].",
                "In `opening_averment`, write the formal deponent oath introduction ('मैं शपथग्रहीता / शपथकर्ता ... सत्यनिष्ठा से शपथपूर्वक निम्नलिखित बयान करता हूँ :-').",
                "In `pleadings`, write each sworn statement starting with 'यह कि ...' without numbering them yourself, preserving every specific detail (Roll No., Registration No., OTR No., Course, Session, Dates, Amounts).",
                "Leave `prayer` as [] (put any final undertaking or humble request to the authority in the final paragraph of `pleadings`).",
                "Write `verification` in the name of `उपरोक्त शपथग्रहीता / शपथकर्ता` and `signature_block` with `हस्ताक्षर शपथग्रहीता / शपथकर्ता`.",
            ]
        elif style_mode == "template_mode":
            context_to_send = context
            mode_rules = [
                f"DRAFTING STYLE MODE: PURCHASED 500+ LEGAL TEMPLATES MODE (Selected Master Template: {selected_tpl}).",
                "Use [MASTER TEMPLATE 1] from CORPUS_REFERENCE as the direct Master Template for this draft.",
                "PRESERVE THE FULL PARAGRAPH ELABORATION AND COMPLETE SECTION STRUCTURE OF THE TEMPLATE: never compress into short 1-sentence summaries.",
                "Write every numeric value in digits followed by its Hindi words in parentheses, e.g., 412 (चार सौ बारह), 0.250 (शून्य दशमलव दो पाँच शून्य), 2 (दो) बिस्वा, 52 (बावन) वर्ष.",
                "For Written Statement (जवाब दावा), reply 1-to-1 to each paragraph (वादपत्र की धारा 1, धारा 2, ...) of the Vaad Patra on purely factual grounds in Part 1, then include 'अतिरिक्त कथन (उजरात मजीद)' followed by the defendants' true factual defense and factual maintainability pleas WITHOUT citing any legal sections, CPC Orders/Rules, BNSS, or statutory Acts.",
            ]
        elif style_mode == "dad_style":
            context_to_send = context
            mode_rules = [
                f"DRAFTING STYLE MODE: DAD'S 306 ORIGINAL FILES MODE (Selected Reference: {selected_tpl}).",
                "Replicate Advocate B.D. Shukla's complete court structure, rich multi-sentence elaboration, and exact vocabulary ('यह कि वादपत्र की धारा 1 (एक) का लेख जिस तरह से लेखबद्ध है स्वीकार नहीं है और इंकार है', 'मालिक काबिज दखील संक्रमणीय भूमिधर', 'नजरी नक्शा', 'चौहद्दी', 'कहा-कहलवाया व समझाया किन्तु प्रतिवादीगण इंकारी हैं', 'अतिरिक्त कथन (उजरात मजीद)') from [DAD ORIGINAL DRAFT 1] and CLEAN_STYLE_ANCHOR, WITHOUT citing any legal sections, CPC, or BNSS in pleadings.",
                "Replace all historical case details with the current case details from CASE_FACTS.",
            ]
        else:
            context_to_send = context
            mode_rules = [
                f"DRAFTING STYLE MODE: HYBRID MIX (Template Structure + Dad's Language: {selected_tpl}).",
                "Combine the complete structural clauses and comprehensive prayer of SECTION 1 with Advocate B.D. Shukla's authentic court vocabulary and transition style ('यह कि ...') from SECTION 2, WITHOUT citing any legal sections, CPC, or BNSS in pleadings.",
            ]

        package = {
            "DOCUMENT_TYPE": label,
            "SELECTED_STYLE_MODE": STYLE_NAMES.get(style_mode, style_mode),
            "SELECTED_TEMPLATE_FILE": selected_tpl,
            "CASE_FACTS": {k: v for k, v in state.facts.items() if k not in {"raw_source_text", "raw_source_pages"}},
            "CORPUS_REFERENCE": context_to_send,
            "CLEAN_STYLE_ANCHOR": style_anchor if (style_mode != "template_mode" and state.document_type != "affidavit") else "",
            "SYSTEM_RULES": BASE_RULES + mode_rules + [
                "Never output legacy-font artifacts or mixed Latin characters inside Hindi words.",
                "Use standard Unicode Devanagari Hindi in every Hindi sentence.",
            ],
            "OUTPUT_REQUIREMENTS": {
                "court_heading": "Court or Authority heading (e.g. 'न्यायालय श्रीमान सिविल जज (प्रवर खंड), औरैया' or 'समक्ष : कुलसचिव महोदय, ... / सक्षम प्राधिकारी, ...').",
                "case_heading": "Formal suit/case number line ONLY for court cases (leave empty '' for non-court/administrative/scholarship affidavits). Never put party names here.",
                "parties": "Each individual plaintiff and each individual defendant as a SEPARATE string item for court cases (leave empty [] for non-court/administrative affidavits).",
                "title": "Formal underlined document title (e.g. 'शपथ-पत्र (AFFIDAVIT)' for affidavits).",
                "opening_averment": "Formal opening address or Deponent oath introduction ('मैं शपथग्रहीता / शपथकर्ता ... सत्यनिष्ठा से शपथपूर्वक निम्नलिखित बयान करता हूँ :-').",
                "pleadings": "Numbered body paragraphs ('यह कि ...') without leading numbers and WITHOUT any CPC, BNSS, or statutory section citations.",
                "prayer": "Comprehensive multi-clause reliefs for Plaint/WS/Application; leave empty [] for Affidavit.",
                "verification": "Formal verification body text WITHOUT the heading word 'सत्यापन' (in the name of Deponent for Affidavit, Defendants for Written Statement, or Plaintiff for Plaint).",
                "signature_block": "Formal place, date, party/deponent signature line(s), and advocate/identification block.",
                "layout": "Optional page-layout metadata.",
            },
        }
        if prior_plaint_paragraphs and state.document_type in {"written_statement", "affidavit"}:
            package["PLAINT_PARAGRAPHS_TO_REBUT"] = prior_plaint_paragraphs

        system = (
            style
            + "\n\nYou are the FINAL DRAFTING ENGINE for Indian Legal & Judicial Drafting.\n"
            + "\n".join(f"- {x}" for x in (BASE_RULES + mode_rules))
        )
        errors = []
        for attempt in range(2):
            raw_draft = self.client.generate_json(
                system=system,
                prompt=json.dumps(package, ensure_ascii=False, indent=2),
                schema=SCHEMA,
                thinking_level=os.getenv("GEMINI_DRAFTING_THINKING_LEVEL", "high"),
                max_output_tokens=16384,
            )
            normalized_raw = _normalize_draft_structure(
                raw_draft, state.facts, document_type=state.document_type, style_mode=style_mode
            )
            draft = format_draft_numbers(sanitize_draft_unicode(normalized_raw))
            errors = self.validate(draft, state.facts, state.document_type)
            if not errors:
                state.status = "ready"
                return draft
            package["SYSTEM_RULES"].append(
                "Previous candidate failed validation: " + "; ".join(errors) + ". Regenerate without changing approved facts."
            )
        raise RuntimeError("Generated draft failed deterministic validation: " + "; ".join(errors))

    def validate(self, draft, facts, document_type):
        if (draft.get("layout") or {}).get("single_page_mode"):
            return []
        errors = []
        for key in ("title", "pleadings", "signature_block"):
            if not draft.get(key):
                errors.append(f"missing {key}")
        text = json.dumps(draft, ensure_ascii=False)
        hindi_chars = sum("\u0900" <= c <= "\u097f" for c in text)
        if hindi_chars >= 80 and legacy_corruption_score(text) > 0.18:
            errors.append("draft contains mixed legacy-font Hindi artifacts; regenerate using clean Unicode Hindi")
        if not _has_identifiable_party(draft, facts):
            errors.append("draft does not contain identifiable supplied party/deponent information")
        return errors