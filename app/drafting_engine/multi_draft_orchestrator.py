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

SECTION_DIVIDER_HEADING = "अतिरिक्त कथन एवं विशेष वैधानिक आपत्तियाँ"

BASE_RULES = [
    "CASE_FACTS are the sole authority for case-specific names, dates, plot/Gata numbers, roll/registration numbers, boundaries, and events. Never copy old party names or unrelated past transactions from retrieved templates.",
    "CAUSE-TITLE PARTY FORMATTING (`parties`): Never write 'वादी:', 'वादीगण:', 'प्रतिवादी:', or 'प्रतिवादीगण:' at the start of any party line (the renderer places `.................... वादी` and `.................... प्रतिवादीगण` on the right automatically). Never merge multiple plaintiffs or multiple defendants into a single string—put each person as a separate item in `parties` so they are numbered individually (`1 (एक). ...`, `2 (दो). ...`).",
    "MANDATORY BILINGUAL NUMBER FORMAT: Every factual numerical value (Gata/Khasra/Bhoomi number, area in hectare/biswa/bigha/decimal, age, date, time, money amount, fraction, and Vaad Patra paragraph number) MUST be written in digits followed by its Hindi words in parentheses, e.g., 'वादपत्र की धारा 1 (एक)', 'गाटा संख्या 412 (चार सौ बारह)', 'रकबा 0.250 (शून्य दशमलव दो पाँच शून्य) हेक्टेयर', '2 (दो) बिस्वा', 'उम्र लगभग 52 (बावन) वर्ष', 'दिनांक 12 अक्टूबर 2026 (बारह अक्टूबर दो हजार छब्बीस)', 'प्रातः 10:00 (दस) बजे'. Never write bare number words like 'दो बिस्वा' without the digit '2 (दो) बिस्वा'.",
    "NEVER write thin, 1-sentence summaries. Every pleading or affidavit paragraph must be written in rich, authoritative, multi-sentence formal Hindi ('यह कि ...').",
    "FOR WRITTEN STATEMENT (जवाब दावा) TWO-PART STRUCTURE: (1) In Part 1 (para-wise replies to the Vaad Patra), reply strictly 1-to-1 to the numbered paragraphs of the Vaad Patra ('यह कि वादपत्र की धारा 1 (एक) का कथन...', 'यह कि वादपत्र की धारा 2 (दो) का कथन...', etc.) on purely factual grounds—NEVER cite legal Acts or statutory Sections inside the Part 1 para-wise replies, and NEVER put a 'भाग प्रथम' heading before Paragraph 1. (2) Immediately after the para-wise replies, insert the exact section heading item 'अतिरिक्त कथन एवं विशेष वैधानिक आपत्तियाँ', followed by continuously numbered paragraphs setting out the defendants' true factual defense (अतिरिक्त कथन) AND all special statutory objections (विशेष वैधानिक आपत्तियाँ: धारा 34, 38, 39 एवं धारा 41(एच),(आई),(जे) विशिष्ट अनुतोष अधिनियम, 1963; आदेश 7 नियम 3, नियम 11(क) व (घ) एवं नियम 14 सिविल प्रक्रिया संहिता; धारा 24, 134, 144 सपठित धारा 206 उ०प्र० राजस्व संहिता, 2006 एवं धारा 9 सिविल प्रक्रिया संहिता; आदेश 1 नियम 9 व 10 सिविल प्रक्रिया संहिता; धारा 115 साक्ष्य अधिनियम एवं धारा 3 परिसीमा अधिनियम; तथा धारा 35-ए सिविल प्रक्रिया संहिता विशेष हर्जाना).",
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

_LEADING_ROLE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*)?"
    r"(?:प्रतिवादीगण|प्रतिवादिनीगण|प्रतिवादिनी|प्रतिवादी|विपक्षीगण|विपक्षी|वादीगण|वादिनी|वादी|प्रार्थीगण|प्रार्थिनी|प्रार्थी|उत्तरदातागण|उत्तरदाता|शपथग्रहीता|शपथकर्ता)"
    r"(?:\s*(?:संख्या|सं०|सं\.|नं०|नं\.)\s*\d+(?:\s*\([^)]+\))?(?:\s*(?:व|एवं|तथा)\s*\d+(?:\s*\([^)]+\))?)?)?"
    r"\s*[:\-–—]+\s*)+"
)

_TRAILING_ROLE_SUFFIX_RE = re.compile(
    r"\s*(?:[,;|\-–—.]+\s*)?"
    r"(?:प्रतिवादीगण|प्रतिवादिनीगण|प्रतिवादिनी|प्रतिवादी|विपक्षीगण|विपक्षी|वादीगण|वादिनी|वादी|प्रार्थीगण|प्रार्थिनी|प्रार्थी|उत्तरदातागण|उत्तरदाता|शपथग्रहीता|शपथकर्ता)"
    r"(?:\s*(?:संख्या|सं०|सं\.|नं०|नं\.)\s*\d+(?:\s*\([^)]+\))?(?:\s*(?:व|एवं|तथा)\s*\d+(?:\s*\([^)]+\))?)?)?"
    r"[।.]?\s*$"
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
    """Remove both leading ('वादी: ...', 'प्रतिवादीगण: ...') and trailing ('... — वादी') role labels."""
    s = str(line or "").strip()
    s = _LEADING_ROLE_PREFIX_RE.sub("", s).strip()
    s = _TRAILING_ROLE_SUFFIX_RE.sub("", s).strip()
    return s


def _expand_merged_party_lines(lines: list[str]) -> list[str]:
    """Split a merged compound party line like 'दिनेश कुमार यादव पुत्र रामेश्वर यादव तथा संतोष यादव पुत्र रामेश्वर यादव, दोनों निवासीगण ग्राम भसौरा...' into individual party lines."""
    expanded: list[str] = []
    for line in lines:
        s = _strip_party_role_affixes(line)
        if not s:
            continue
        s_no_num = re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", s).strip()
        m = re.match(
            r"^(?P<p1>.+?\b(?:पुत्र|पत्नी|पुत्री|विधवा)\s+[^,]+?)\s+(?:तथा|एवं|व|और)\s+"
            r"(?P<p2>.+?\b(?:पुत्र|पत्नी|पुत्री|विधवा)\s+[^,]+?)\s*,?\s*"
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
            is_plural_pl = any(w in p_clean for w in ("दोनों", "तीनों", "निवासीगण", "पुत्रगण", "वादीगण"))
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
            is_plural_df = any(w in d_clean for w in ("दोनों", "तीनों", "निवासीगण", "पुत्रगण", "प्रतिवादीगण"))
            out.append(d_clean)
            out.append(".................... प्रतिवादीगण" if is_plural_df else ".................... प्रतिवादी")

    for o in other:
        o_clean = _strip_party_role_affixes(o)
        if o_clean:
            out.append(o_clean)
    return out


def _clean_short_name(raw: str) -> str:
    s = _strip_party_role_affixes(str(raw or ""))
    s = re.sub(r"^\s*मैं\s*,?\s*(?:शपथग्रहीता|शपथकर्ता)?\s*", "", s).strip()
    m = re.split(r"\s+(?:पुत्र|पत्नी|पुत्री|विधवा|उम्र|आयु|निवासी)\b|,", s, maxsplit=1)
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
    "विशिष्ट अनुतोष अधिनियम",
    "सिविल प्रक्रिया संहिता",
    "जाप्ता दीवानी",
    "सी०पी०सी०",
    "सी.पी.सी.",
    "उत्तर प्रदेश राजस्व संहिता",
    "भारतीय परिसीमा अधिनियम",
    "परिसीमा अधिनियम",
    "उत्तर प्रदेश न्यायालय शुल्क अधिनियम",
    "वाद मूल्यांकन अधिनियम",
    "सम्पत्ति अंतरण अधिनियम",
    "आदेश 7 नियम",
    "आदेश 1 नियम",
    "आदेश 8 नियम",
    "आदेश 39 नियम",
    "धारा 35ए",
    "धारा 35-ए",
    "धारा 35अ",
    "धारा 41(",
    "धारा 41 (",
    "धारा 206",
    "धारा 115",
    "धारा 101",
    "धारा 104",
)


def _structure_written_statement_two_parts(pleadings: list[str], facts: dict[str, Any]) -> tuple[list[str], int, int]:
    """Structure a Written Statement (जवाब दावा) into:
    1. Part 1: Pure 1-to-1 para-wise factual replies to the Vaad Patra ('वादपत्र की धारा 1 (एक)...') with ZERO statutory Act citations.
    2. Centered Section Heading: 'अतिरिक्त कथन एवं विशेष वैधानिक आपत्तियाँ'
    3. Part 2: Factual additional statements (अतिरिक्त कथन) + Special Statutory Objections (विशेष वैधानिक आपत्तियाँ).
    Returns (structured_pleadings_list, part1_count, total_numbered_paragraphs).
    """
    part1_replies: list[str] = []
    part2_objections: list[str] = []
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
        has_statute = any(m in p_body for m in _STATUTORY_CITATION_MARKERS)

        if not in_part2 and (is_plaint_para_reply or not has_statute):
            sentences = [s.strip() for s in re.split(r"(?<=।)\s*", p_body) if s.strip()]
            kept: list[str] = []
            for sent in sentences:
                sent_has_statute = any(m in sent for m in _STATUTORY_CITATION_MARKERS)
                sent_has_plaint_dhara = bool(re.search(r"(?:वाद\s*पत्र|वादपत्र)\s*(?:की|के)\s*(?:धारा|पैरा)", sent))
                if sent_has_statute and not sent_has_plaint_dhara:
                    continue
                if sent_has_statute and sent_has_plaint_dhara:
                    sent = re.sub(
                        r"\s*(?:अंतर्गत|के\s*अंतर्गत|सपठित)?\s*(?:धारा|आदेश)\s*\d+[^।]*?(?:अधिनियम|संहिता|सी०पी०सी०|जाप्ता दीवानी)[^।]*",
                        "",
                        sent,
                    ).strip()
                    if sent and not sent.endswith("।"):
                        sent += "।"
                kept.append(sent)
            if kept:
                clean_p1 = " ".join(kept).strip()
                if not clean_p1.startswith("यह कि"):
                    clean_p1 = "यह कि " + clean_p1
                if not clean_p1.endswith(("।", ".", "!", "?")):
                    clean_p1 += "।"
                part1_replies.append(clean_p1)
        else:
            in_part2 = True
            clean_p2 = p_body
            if not clean_p2.startswith("यह कि"):
                clean_p2 = "यह कि " + clean_p2
            if not clean_p2.endswith(("।", ".", "!", "?")):
                clean_p2 += "।"
            part2_objections.append(clean_p2)

    joined_p2 = "\n".join(part2_objections)
    joined_all = "\n".join(part1_replies + part2_objections)
    all_facts_text = json.dumps(facts, ensure_ascii=False)
    is_agri_or_gata = any(
        k in joined_all or k in all_facts_text
        for k in ("गाटा", "खसरा", "भूमिधर", "खतौनी", "कृषि", "बिस्वा", "हेक्टेयर", "चकमार्ग", "मेड़")
    )

    if not any("वास्तविक तथ्य" in x or "पेशबंदी" in x for x in part2_objections):
        part2_objections.insert(
            0,
            "यह कि वास्तविक तथ्य यह है कि प्रतिवादीगण शांतिप्रिय एवं कानून का पालन करने वाले नागरिक हैं तथा अपनी स्वयं की वैध भूमि पर शांतिपूर्वक काबिज चले आ रहे हैं। "
            "प्रतिवादीगण ने वादी की किसी वैध भूमि या मेड़ को कभी क्षति नहीं पहुंचाई और न ही किसी प्रकार के अवैध कब्जे या निर्माण का प्रयास किया; वादी ने बिना किसी सक्षम सीमांकन के महज पेशबंदी एवं प्रतिवादीगण पर नाजायज दबाव बनाने की नीयत से यह मनगढ़ंत मुकदमा प्रस्तुत किया है।",
        )
        joined_p2 = "\n".join(part2_objections)

    if "धारा 41" not in joined_p2 and "41(" not in joined_p2:
        part2_objections.append(
            "यह कि विशेष वैधानिक आपत्ति (अंतर्गत धारा 34, 38, 39 एवं धारा 41(एच), 41(आई) व 41(जे) विशिष्ट अनुतोष अधिनियम, 1963) : "
            "वादी द्वारा प्रस्तुत वाद विशिष्ट अनुतोष अधिनियम, 1963 की धारा 41(एच), 41(आई) एवं 41(जे) सपठित धारा 34 व 38 के आज्ञापक प्रावधानों से पूर्णतः बाधित है। "
            "जहाँ कथित विवादित भूमि के स्वत्व, क्षेत्रफल, सीमांकन और आधिपत्य पर ही गंभीर विवाद हो, वहाँ बिना स्वत्व की उद्घोषणा (धारा 34) एवं सक्षम सीमांकन के मात्र स्थायी अथवा आदेशात्मक निषेधाज्ञा का वाद पोषणीय नहीं है; "
            "वादी को सक्षम न्यायालय/अधिकारी के समक्ष समान रूप से प्रभावकारी वैकल्पिक उपचार (धारा 41(एच)) उपलब्ध है, वादी स्वच्छ व निर्मल हाथों से माननीय न्यायालय के समक्ष उपस्थित नहीं हुआ है (धारा 41(आई)), तथा कथित विवादित अंश पर वादी का कोई व्यक्तिगत विधिक अधिकार या स्थापित कब्जा नहीं है (धारा 41(जे))।"
        )

    if "आदेश 7 नियम 3" not in joined_p2 or "आदेश 7 नियम 11" not in joined_p2:
        part2_objections.append(
            "यह कि विशेष वैधानिक आपत्ति (अंतर्गत आदेश 7 नियम 3, आदेश 7 नियम 11(क) व (घ) एवं आदेश 7 नियम 14 सिविल प्रक्रिया संहिता, 1908) : "
            "वादी ने वादपत्र में कथित विवादित भूमि की सटीक भुजाओं, लंबाई-चौड़ाई, सर्वे नंबर के विशिष्ट सीमांकन तथा प्रमाणित पैमाना नक्शे का उल्लेख नहीं किया है, जिससे वाद आदेश 7 नियम 3 सिविल प्रक्रिया संहिता के आज्ञापक प्रावधानों से बाधित है। "
            "साथ ही वादपत्र के कथनों से प्रतिवादीगण के विरुद्ध कोई वास्तविक व वैध वाद हेतुक प्रकट नहीं होता है और वाद विधि द्वारा स्पष्ट रूप से बाधित होने के कारण आदेश 7 नियम 11(क) एवं 11(घ) सिविल प्रक्रिया संहिता के अंतर्गत प्रारंभिक स्तर पर ही निरस्त किए जाने योग्य है।"
        )

    if is_agri_or_gata and "धारा 206" not in joined_p2:
        part2_objections.append(
            "यह कि क्षेत्राधिकार संबंधी विशेष वैधानिक आपत्ति (अंतर्गत धारा 24, 134 एवं 144 सपठित धारा 206 उत्तर प्रदेश राजस्व संहिता, 2006 एवं धारा 9 सिविल प्रक्रिया संहिता, 1908) : "
            "वादग्रस्त आराजी कृषि/राजस्व गाटा भूमि से संबंधित है तथा कथित मेड़ तोड़ने, रकबा कम होने अथवा सीमा विवाद (पैमाइश/मेड़बंदी) के निर्धारण का अनन्य क्षेत्राधिकार उत्तर प्रदेश राजस्व संहिता, 2006 की धारा 24 एवं धारा 134/144 के अंतर्गत केवल सक्षम राजस्व न्यायालय (उपजिलाधिकारी) को प्राप्त है। "
            "उत्तर प्रदेश राजस्व संहिता, 2006 की धारा 206 सपठित धारा 9 सिविल प्रक्रिया संहिता के अंतर्गत ऐसे राजस्व एवं सीमांकन विवादों के संबंध में दीवानी न्यायालय (सिविल कोर्ट) का क्षेत्राधिकार स्पष्ट रूप से वर्जित एवं बाधित है।"
        )

    if "आदेश 1 नियम 9" not in joined_p2 and "धारा 115" not in joined_p2:
        part2_objections.append(
            "यह कि विशेष वैधानिक आपत्ति (अंतर्गत आदेश 1 नियम 9 व 10 सिविल प्रक्रिया संहिता, धारा 115 भारतीय साक्ष्य अधिनियम तथा धारा 3 भारतीय परिसीमा अधिनियम, 1963) : "
            "वादी का वाद आवश्यक पक्षकारों (यथा ग्राम सभा, उत्तर प्रदेश सरकार एवं संबंधित सहखातेदारों) को पक्षकार न बनाए जाने के कारण आदेश 1 नियम 9 के परंतुक एवं नियम 10 सिविल प्रक्रिया संहिता के अंतर्गत असंयोजन एवं कुसंयोजन के दोष से ग्रस्त है। "
            "इसके अतिरिक्त दावा वादी भारतीय साक्ष्य अधिनियम की धारा 115 के अंतर्गत विबंधन, परित्याग एवं मौन सहमति के सिद्धांत से तथा भारतीय परिसीमा अधिनियम, 1963 की धारा 3 के अंतर्गत कालबाधित होने के कारण भी निरस्त होने योग्य है।"
        )

    if "35ए" not in joined_p2 and "35-ए" not in joined_p2 and "35अ" not in joined_p2:
        part2_objections.append(
            "यह कि विशेष वैधानिक आपत्ति (अंतर्गत वाद मूल्यांकन अधिनियम, 1887, धारा 7(iv-ख) उत्तर प्रदेश न्यायालय शुल्क अधिनियम, 1870 एवं धारा 35-ए सिविल प्रक्रिया संहिता, 1908) : "
            "वादी ने वाद का मूल्यांकन जानबूझकर अत्यंत कम किया है और अपर्याप्त न्याय शुल्क अदा किया है। "
            "वादी द्वारा प्रतिवादीगण को अनावश्यक रूप से हैरान व परेशान करने हेतु यह मिथ्या, मनगढ़ंत व निराधार वाद प्रस्तुत किए जाने के कारण वादी का वाद सव्यय खारिज कर प्रतिवादीगण को सिविल प्रक्रिया संहिता की धारा 35-ए के अंतर्गत रु० 25,000/- (पच्चीस हजार) विशेष हर्जाना दिलाया जाना न्यायसंगत है।"
        )

    structured = part1_replies + [SECTION_DIVIDER_HEADING] + part2_objections
    part1_count = len(part1_replies)
    total_numbered = len(part1_replies) + len(part2_objections)
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
    deponent_full = re.sub(r"^\s*मैं\s*,?\s*(?:शपथग्रहीता|शपथकर्ता)?\s*", "", deponent_full).strip()

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
        # Never put "मूल वाद संख्या ............ सन् 2026" or "वादी बनाम प्रतिवादी" on an Administrative/University/Notary Affidavit
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
        p_no_num = re.sub(r"^\s*(?:प्रस्तर|धारा|पैरा)?\s*\d+\s*(?:\([^)]*\))?\s*[:.\-–—]+\s*", "", p).strip()
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

    if document_type == "written_statement" and out.get("title"):
        t_clean = re.sub(r"\s*,?\s*अंतर्गत\s+(?:आदेश|धारा)\s+.*$", "", out["title"]).strip()
        out["title"] = t_clean or "प्रतिवादीगण की ओर से वादपत्र का लिखित कथन / जवाब दावा"

    # Ensure mandatory Jurisdiction & Valuation/Court-Fee paragraphs in Dava Plaint
    if document_type == "dava_plaint" and out.get("pleadings"):
        pleadings = list(out["pleadings"])
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
            factual_end = (ws_part1_count + 1) if (document_type == "written_statement" and ws_part1_count > 0) else max(1, total_p - 3)
            factual_end = min(factual_end, max(1, total_p - 1))
            legal_start = min(total_p, factual_end + 1)
            doc_label = "प्रतिवाद पत्र / जवाब दावा (लिखित कथन)" if document_type == "written_statement" else "वाद पत्र"
            verb = "सत्यापित करते हैं" if "गण" in role_label else "सत्यापित करता हूँ"
            pronoun = "हम" if "गण" in role_label else "मैं"
            my_word = "हमारी" if "गण" in role_label else "मेरी"
            our_word = "हमारे" if "गण" in role_label else "मेरे"
            ver = (
                f"{pronoun} कि {party_name}, {role_label}, आज दिनांक ............ को स्थान {place_str} पर यह {verb} कि "
                f"{doc_label} के पैरा नं० 1 से {factual_end} तक के कथन {my_word} निजी जानकारी में सत्य व सही हैं तथा पैरा नं० {legal_start} से {total_p} तक के कथन "
                f"अभिलेखों एवं विधिक सलाह के आधार पर {our_word} विश्वास में सत्य हैं। कोई भी तथ्य असत्य नहीं है और न ही कोई महत्वपूर्ण तथ्य छिपाया गया है।"
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

    # Always apply deterministic bilingual number formatting (digits + Hindi words in parentheses) across all styles
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
        if state.document_type in {"affidavit", "evidence_pw_affidavit"} and not state.facts.get("deponent"):
            for fallback_key in ("plaintiffs", "parties", "sender"):
                vals = state.facts.get(fallback_key, [])
                if isinstance(vals, list) and vals:
                    state.facts["deponent"] = vals[0]
                    break
                elif isinstance(vals, str) and vals.strip():
                    state.facts["deponent"] = vals.strip()
                    break
            if not state.facts.get("deponent") and state.facts.get("facts"):
                state.facts["deponent"] = "शपथग्रहीता / शपथकर्ता"
        if state.document_type == "affidavit" and not state.facts.get("purpose") and state.facts.get("facts"):
            state.facts["purpose"] = "प्रस्तुत प्रकरण / विषय के संबंध में शपथपूर्वक कथन प्रस्तुत करने हेतु"
        missing = missing_fields(state.facts, state.document_type)
        if missing:
            state.status = "collecting"
            return {"status": "needs_information", "missing": missing, "questions": next_questions(state.facts, state.document_type)}
        state.status = "ready"
        return {"status": "ready", "facts": state.facts}

    def extract_and_collect(self, state: CaseState, text: str):
        extracted = extract_case_facts(
            text,
            current_facts=state.facts,
            document_type=state.document_type,
            user_history=state.user_messages(limit=int(os.getenv("GEMINI_INTAKE_HISTORY_MESSAGES", "40"))),
        )
        return self.collect(state, extracted)

    def refresh_intake(self, state: CaseState):
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
        if missing_fields(state.facts, state.document_type):
            raise ValueError("Required facts are still missing.")
        corpus_type, prompt_file, label = TYPE_META[state.document_type]
        style = (PROMPTS / prompt_file).read_text(encoding="utf-8")
        style_mode = (state.style_mode or "template_mode").strip().lower()
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
                "For Written Statement, reply 1-to-1 to each paragraph (वादपत्र की धारा 1, धारा 2, ...) of the Vaad Patra without statutory citations in Part 1, then include 'अतिरिक्त कथन एवं विशेष वैधानिक आपत्तियाँ' followed by the additional factual pleas and special statutory objections.",
            ]
        elif style_mode == "dad_style":
            context_to_send = context
            mode_rules = [
                f"DRAFTING STYLE MODE: DAD'S 306 ORIGINAL FILES MODE (Selected Reference: {selected_tpl}).",
                "Replicate Advocate B.D. Shukla's complete court structure, rich multi-sentence elaboration, and exact vocabulary ('यह कि वादपत्र की धारा 1 (एक) का लेख जिस तरह से लेखबद्ध है स्वीकार नहीं है और इंकार है', 'मालिक काबिज दखील संक्रमणीय भूमिधर', 'नजरी नक्शा', 'चौहद्दी', 'कहा-कहलवाया व समझाया किन्तु प्रतिवादीगण इंकारी हैं', 'अतिरिक्त कथन एवं विशेष वैधानिक आपत्तियाँ') from [DAD ORIGINAL DRAFT 1] and CLEAN_STYLE_ANCHOR.",
                "Replace all historical case details with the current case details from CASE_FACTS.",
            ]
        else:
            context_to_send = context
            mode_rules = [
                f"DRAFTING STYLE MODE: HYBRID MIX (Template Structure + Dad's Language: {selected_tpl}).",
                "Combine the complete structural clauses and comprehensive prayer of SECTION 1 with Advocate B.D. Shukla's authentic court vocabulary and transition style ('यह कि ...') from SECTION 2.",
            ]

        package: dict[str, Any] = {
            "DOCUMENT_TYPE": label,
            "SELECTED_STYLE_MODE": STYLE_NAMES.get(style_mode, style_mode),
            "SELECTED_TEMPLATE_FILE": selected_tpl,
            "CASE_FACTS": state.facts,
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
                "pleadings": "Numbered body paragraphs ('यह कि ...') without leading numbers.",
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
