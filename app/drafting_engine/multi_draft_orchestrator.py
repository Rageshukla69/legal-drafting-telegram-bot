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
    "evidence_pw_affidavit": ("evidence_pw_affidavit", "evidence_specialist.txt", "Evidence / PW Affidavit"),
    "affidavit": ("affidavit", "affidavit_specialist.txt", "Affidavit / शपथपत्र"),
    "legal_notice": ("legal_notice", "legal_notice_specialist.txt", "Legal Notice / विधिक नोटिस"),
    "other_civil": ("other_civil", "other_civil_specialist.txt", "Other Civil Draft / अन्य सिविल ड्राफ्ट"),
}
ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT.parents[1] / "prompts"
SCHEMA = json.loads((ROOT / "draft_schema.json").read_text(encoding="utf-8"))

BASE_RULES = [
    "CASE_FACTS are the sole authority for case-specific names, dates, plot/Gata numbers, boundaries, and events. Never copy old party names or unrelated past transactions from retrieved templates.",
    "STRICT PROHIBITION ON STATUTORY / LEGAL 'DHARA' CITATIONS IN PLEADINGS: Never cite any legal Act, Statute, Code, Order, Rule, or statutory Section (such as भारतीय साक्ष्य अधिनियम, विशिष्ट अनुतोष अधिनियम, सिविल प्रक्रिया संहिता, उत्तर प्रदेश राजस्व संहिता, परिसीमा अधिनियम, न्यायालय शुल्क अधिनियम, आदेश 7 नियम 11, धारा 35, धारा 41, धारा 35ए, etc.) inside `pleadings`. Pleadings must state ONLY material facts and factual rebuttals.",
    "CAUSE-TITLE PARTY FORMATTING (`parties`): Never write 'वादी:', 'वादीगण:', 'प्रतिवादी:', or 'प्रतिवादीगण:' at the start of any party line (the renderer places `.................... वादी` and `.................... प्रतिवादीगण` on the right automatically). Never merge multiple plaintiffs or multiple defendants into a single string—put each person as a separate item in `parties` so they are numbered individually (`1 (एक). ...`, `2 (दो). ...`).",
    "MANDATORY BILINGUAL NUMBER FORMAT: Every factual numerical value (Gata/Khasra/Bhoomi number, area in hectare/biswa/bigha/decimal, age, date, time, money amount, fraction, and Vaad Patra paragraph number) MUST be written in digits followed by its Hindi words in parentheses, e.g., 'वादपत्र की धारा 1 (एक)', 'गाटा संख्या 412 (चार सौ बारह)', 'रकबा 0.250 (शून्य दशमलव दो पाँच शून्य) हेक्टेयर', '2 (दो) बिस्वा', 'उम्र लगभग 52 (बावन) वर्ष', 'दिनांक 12 अक्टूबर 2026 (बारह अक्टूबर दो हजार छब्बीस)', 'प्रातः 10:00 (दस) बजे'. Never write bare number words like 'दो बिस्वा' without the digit '2 (दो) बिस्वा'.",
    "NEVER write thin, 1-sentence summaries. Every pleading paragraph must be written in rich, authoritative, multi-sentence court Hindi ('यह कि ...') matching the depth and professional articulation of the selected Master Template or Advocate B.D. Shukla's original court drafts.",
    "FOR WRITTEN STATEMENT (जवाब दावा): Reply strictly 1-to-1 to the numbered paragraphs (धाराएं) of the Vaad Patra ('यह कि वादपत्र की धारा 1 (एक) का कथन...', 'यह कि वादपत्र की धारा 2 (दो) का कथन...', etc.). Never insert standalone heading items (such as 'भाग प्रथम — धारावार उत्तर' or 'अतिरिक्त कथन') into `pleadings`.",
    "Preserve event status exactly: attempt is not completion; threat is not dispossession; allegation is not an established fact.",
    "If CASE_FACTS contains custom_add_points, include every requested point as a proper paragraph/clause.",
    "If CASE_FACTS contains custom_remove_points, omit those topics/clauses from the draft.",
    "Never number paragraphs yourself. The deterministic renderer assigns continuous numbering.",
    "Return only JSON matching the supplied schema.",
]

_PARTY_STOPWORDS = {
    "वादी", "वादीगण", "वादिनी", "प्रतिवादी", "प्रतिवादीगण", "प्रतिवादिनी", "विपक्षी", "विपक्षीगण",
    "प्रार्थी", "प्रार्थीगण", "उम्र", "करीब", "वर्ष", "पुत्र", "पुत्री", "पत्नी", "पति", "विधवा",
    "स्व", "स्व०", "स्वर्गीय", "श्री", "श्रीमती", "सुश्री", "कुमारी", "निवासी", "निवासीगण",
    "मूल", "वर्तमान", "पता", "ग्राम", "मौजा", "मौज़ा", "डाकखाना", "पोस्ट", "परगना", "तहसील",
    "जिला", "थाना", "वार्ड", "संख्या", "मकान", "नंबर", "बनाम",
    "plaintiff", "plaintiffs", "defendant", "defendants", "respondent", "applicant",
    "petitioner", "deponent", "sender", "recipient", "age", "about", "approx", "years",
    "son", "daughter", "wife", "husband", "widow", "late", "shri", "smt", "resident",
    "village", "post", "tehsil", "pargana", "district", "ward", "house",
}

_LEADING_ROLE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*)?"
    r"(?:प्रतिवादीगण|प्रतिवादिनीगण|प्रतिवादिनी|प्रतिवादी|विपक्षीगण|विपक्षी|वादीगण|वादिनी|वादी|प्रार्थीगण|प्रार्थिनी|प्रार्थी|उत्तरदातागण|उत्तरदाता)"
    r"(?:\s*(?:संख्या|सं०|सं\.|नं०|नं\.)\s*\d+(?:\s*\([^)]+\))?(?:\s*(?:व|एवं|तथा)\s*\d+(?:\s*\([^)]+\))?)?)?"
    r"\s*[:\-–—]+\s*)+"
)

_TRAILING_ROLE_SUFFIX_RE = re.compile(
    r"\s*(?:[,;|\-–—.]+\s*)?"
    r"(?:प्रतिवादीगण|प्रतिवादिनीगण|प्रतिवादिनी|प्रतिवादी|विपक्षीगण|विपक्षी|वादीगण|वादिनी|वादी|प्रार्थीगण|प्रार्थिनी|प्रार्थी|उत्तरदातागण|उत्तरदाता)"
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
    # Preserve leading item number if present before role label, e.g. '1. प्रतिवादी: दिनेश' -> 'दिनेश'
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
        # Remove leading '1.' or '1 (एक).' before checking if it is a merged compound line
        s_no_num = re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", s).strip()
        # Pattern: "<Person 1 with पुत्र/पत्नी/...> तथा/एवं/व/और <Person 2 with पुत्र/पत्नी/...>, [दोनों] निवासी[गण] <Address>"
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

    # If the model merged multiple parties into 1 line (or omitted them) while `facts` has separated parties, use `facts`
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
    m = re.split(r"\s+(?:पुत्र|पत्नी|पुत्री|विधवा|उम्र|निवासी)\b|,", s, maxsplit=1)
    if m and m[0].strip():
        return re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", m[0].strip())
    return s.strip()


def _extract_party_names_for_doc(facts: dict[str, Any], document_type: str = "dava_plaint") -> tuple[str, str]:
    """Return (short_party_names_str, role_label_str) appropriate for the document_type."""
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


_STANDALONE_HEADING_RE = re.compile(
    r"^\s*(?:भाग\s*(?:प्रथम|द्वितीय|तृतीय|एक|दो|1|2)|"
    r"(?:वादपत्र\s*की\s*)?(?:धारावार|पैरावार|तथ्यवार)\s*(?:प्रतिपक्षी\s*)?(?:उत्तर|जवाब)|"
    r"प्रारम्भिक\s*(?:वैधानिक\s*)?(?:आपत्तियाँ|आपत्तियां|कथन)|"
    r"प्राथमिक\s*(?:आपत्तियाँ|आपत्तियां)|"
    r"अतिरिक्त\s*कथन(?:\s*\(\s*उजरात\s*मजीद\s*\))?|"
    r"विशेष\s*(?:वैधानिक\s*)?(?:कथन|आपत्तियाँ|आपत्तियां)|"
    r"उजरात\s*मजीद)\s*[:\-–—]*\s*$"
)

_INLINE_HEADING_PREFIX_RE = re.compile(
    r"^\s*(?:यह\s*कि\s*)?(?:भाग\s*(?:प्रथम|द्वितीय|1|2)\s*[-–—:]\s*)?"
    r"(?:(?:वादपत्र\s*की\s*)?(?:धारावार|पैरावार|तथ्यवार)\s*(?:/\s*(?:पैरावार|धारावार)\s*)?(?:प्रतिपक्षी\s*)?(?:उत्तर|जवाब)|"
    r"प्रारम्भिक\s*(?:वैधानिक\s*)?(?:आपत्तियाँ|आपत्तियां|कथन)|"
    r"अतिरिक्त\s*कथन(?:\s*\(\s*उजरात\s*मजीद\s*\))?|"
    r"विशेष\s*(?:वैधानिक\s*)?(?:कथन|आपत्ति|आपत्तियाँ|आपत्तियां))"
    r"(?:\s*\([^)]*\))?\s*[:\-–—]+\s*"
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


def _clean_written_statement_pleadings(pleadings: list[str]) -> list[str]:
    """Ensure Written Statement pleadings contain only 1-to-1 para-wise replies to the Vaad Patra and factual defense statements, with zero standalone section headings and zero statutory Act citations."""
    cleaned: list[str] = []
    for raw_p in pleadings:
        p = str(raw_p or "").strip()
        if not p:
            continue
        # Remove leading paragraph numbers if model added any
        p_no_num = re.sub(r"^\s*\d+\s*(?:\([^)]*\))?\s*[:.\-–—]+\s*", "", p).strip()
        # 1. Drop standalone section-heading items (e.g. "भाग प्रथम — वादपत्र की धारावार / पैरावार प्रतिपक्षी उत्तर")
        if _STANDALONE_HEADING_RE.match(p_no_num):
            continue
        if not p_no_num.startswith("यह कि") and len(p_no_num.split()) <= 10 and any(
            w in p_no_num for w in ("भाग प्रथम", "भाग द्वितीय", "धारावार", "पैरावार", "तथ्यवार", "प्रारम्भिक", "अतिरिक्त कथन", "उजरात मजीद")
        ):
            continue

        # 2. Strip inline heading prefix if attached to the start of a paragraph
        p_body = _INLINE_HEADING_PREFIX_RE.sub("", p_no_num).strip()
        if not p_body:
            continue

        # 3. Split into sentences and strip sentences that cite statutory Acts/Orders/Rules/Sections
        #    while preserving every sentence that replies to "वादपत्र की धारा ..."
        sentences = [s.strip() for s in re.split(r"(?<=।)\s*", p_body) if s.strip()]
        kept_sentences: list[str] = []
        for sent in sentences:
            has_statute = any(m in sent for m in _STATUTORY_CITATION_MARKERS)
            has_plaint_dhara = bool(re.search(r"(?:वाद\s*पत्र|वादपत्र)\s*(?:की|के)\s*(?:धारा|पैरा)", sent))
            if has_statute and not has_plaint_dhara:
                continue
            if has_statute and has_plaint_dhara:
                # Strip the statutory clause from the sentence while keeping the Vaad Patra dhara reply
                sent = re.sub(
                    r"\s*(?:अंतर्गत|के\s*अंतर्गत|सपठित)?\s*(?:धारा|आदेश)\s*\d+[^।]*?(?:अधिनियम|संहिता|सी०पी०सी०|जाप्ता दीवानी)[^।]*",
                    "",
                    sent,
                ).strip()
                if sent and not sent.endswith("।"):
                    sent += "।"
            kept_sentences.append(sent)

        if not kept_sentences:
            continue

        final_p = " ".join(kept_sentences).strip()
        final_p = re.sub(r"\bvexatious\b", "मिथ्या एवं तंग करने वाले", final_p, flags=re.I)
        if not final_p.startswith("यह कि"):
            final_p = "यह कि " + final_p
        if not final_p.endswith(("।", ".", "!", "?")):
            final_p += "।"
        cleaned.append(final_p)

    return cleaned


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

    # Clean Written Statement title if it contains statutory Act citations
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

    # Clean Written Statement (जवाब दावा) pleadings: 1-to-1 Vaad Patra dhara replies ONLY, no section headings, no statutory Act citations
    if document_type == "written_statement" and out.get("pleadings"):
        out["pleadings"] = _clean_written_statement_pleadings(list(out["pleadings"]))

    # Clean Prayer clauses: strip redundant lead-ins inside individual sub-clauses
    if out.get("prayer"):
        cleaned_prayer: list[str] = []
        for item in out["prayer"]:
            s = re.sub(
                r"^\s*(?:\([क-ज्ञa-zA-Z0-9]+\)\s*)?(?:अतः\s*)?(?:अत्यन्त\s*विनयपूर्वक\s*)?(?:माननीय\s*न्यायालय\s*से\s*|श्रीमान\s*जी\s*से\s*)?(?:सादर\s*|सविनय\s*)?प्रार्थना\s*(?:की\s*जाती\s*)?है\s*कि\s*[:-]*\s*",
                "",
                str(item).strip(),
            ).strip()
            if document_type == "written_statement":
                s = re.sub(
                    r"\s*अंतर्गत\s+(?:आदेश|धारा)\s+\d+[^,।]*?(?:संहिता|अधिनियम)(?:\s*,\s*\d{4})?(?:\s*सपठित\s*धारा\s*\d+[^,।]*?(?:संहिता|अधिनियम)(?:\s*,\s*\d{4})?)*",
                    "",
                    s,
                ).strip()
            if s:
                cleaned_prayer.append(s)
        out["prayer"] = cleaned_prayer

    # Clean Verification: remove duplicate leading "सत्यापन" heading line and ensure correct party role & paragraph ranges
    ver = str(out.get("verification") or "").strip()
    party_name, role_label = _extract_party_names_for_doc(facts, document_type)
    place_str = str(facts.get("court_complex") or facts.get("place") or "बमुकाम कचहरी बिधूना, औरैया").strip()
    total_p = len(out.get("pleadings") or [])

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
        ws_has_wrong_party = (
            document_type == "written_statement"
            and ("उपरोक्त वादी" in ver or "वाद पत्र के पैरा" in ver or "वादपत्र के पैरा" in ver or (pl_short and pl_short in ver))
        )
        if ws_has_wrong_party or (total_p >= 4 and "पैरा" not in ver and "धारा" not in ver):
            factual_end = max(1, total_p - 2) if document_type == "written_statement" else max(1, total_p - 3)
            legal_start = min(total_p, factual_end + 1)
            doc_label = "प्रतिवाद पत्र / जवाब दावा (लिखित कथन)" if document_type == "written_statement" else "वाद पत्र"
            verb = "सत्यापित करते हैं" if "गण" in role_label else "सत्यापित करता हूँ"
            pronoun = "हम" if "गण" in role_label else "मैं"
            my_word = "हमारी" if "गण" in role_label else "मेरी"
            our_word = "हमारे" if "गण" in role_label else "मेरे"
            ver = (
                f"{pronoun} कि {party_name}, {role_label}, आज दिनांक ............ को स्थान {place_str} पर यह {verb} कि "
                f"{doc_label} के पैरा नं० 1 से {factual_end} तक के कथन {my_word} निजी जानकारी में सत्य व सही हैं तथा पैरा नं० {legal_start} से {total_p} तक के कथन "
                f"अभिलेखों एवं जानकारी के आधार पर {our_word} विश्वास में सत्य हैं। कोई भी तथ्य असत्य नहीं है और न ही कोई महत्वपूर्ण तथ्य छिपाया गया है।"
            )
        out["verification"] = ver

    adv_name = str(facts.get("advocate_name") or "वी०डी० शुक्ला, अधिवक्ता").strip()
    if not out.get("signature_block"):
        sig_role = "प्रतिवादीगण / उत्तरदातागण" if document_type == "written_statement" else "वादी"
        out["signature_block"] = [
            f"स्थान : {place_str}",
            f"दिनांक : ............",
            f"{sig_role} — {party_name}",
            f"द्वारा अधिवक्ता — {adv_name}",
        ]

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
            plaintiffs = state.facts.get("plaintiffs", [])
            if isinstance(plaintiffs, list) and len(plaintiffs) == 1:
                state.facts["deponent"] = plaintiffs[0]
        if state.document_type == "affidavit" and not state.facts.get("purpose"):
            state.facts["purpose"] = "प्रस्तुत वाद/विवाद के संबंध में शपथपूर्वक कथन प्रस्तुत करने हेतु"
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

        # Extract any previously drafted Plaint (Vaad Patra) from state.documents so Written Statement replies 1-to-1 to each Vaad Patra dhara
        prior_plaint_paragraphs: list[str] = []
        for doc_entry in reversed(state.documents or []):
            if doc_entry.get("document_type") == "dava_plaint" and isinstance(doc_entry.get("draft"), dict):
                raw_pl = doc_entry["draft"].get("pleadings") or []
                prior_plaint_paragraphs = [f"वादपत्र की धारा {i}: {p}" for i, p in enumerate(raw_pl, 1)]
                break

        if style_mode == "template_mode":
            mode_rules = [
                f"DRAFTING STYLE MODE: PURCHASED 500+ LEGAL TEMPLATES MODE (Selected Master Template: {selected_tpl}).",
                "Use [MASTER TEMPLATE 1] from CORPUS_REFERENCE as the direct Master Template for this draft.",
                "PRESERVE THE FULL PARAGRAPH ELABORATION AND COMPLETE SECTION STRUCTURE OF THE TEMPLATE: never compress into short 1-sentence summaries.",
                "Write every numeric value in digits followed by its Hindi words in parentheses, e.g., 412 (चार सौ बारह), 0.250 (शून्य दशमलव दो पाँच शून्य), 2 (दो) बिस्वा, 52 (बावन) वर्ष.",
                "Never cite legal Acts/Statutes/Sections in `pleadings`. For Written Statement, reply 1-to-1 to each paragraph (वादपत्र की धारा 1, धारा 2, ...) of the Vaad Patra.",
            ]
        elif style_mode == "dad_style":
            mode_rules = [
                f"DRAFTING STYLE MODE: DAD'S 306 ORIGINAL FILES MODE (Selected Reference: {selected_tpl}).",
                "Replicate Advocate B.D. Shukla's complete court structure, rich multi-sentence elaboration, and exact vocabulary ('यह कि वादपत्र की धारा 1 (एक) का लेख जिस तरह से लेखबद्ध है स्वीकार नहीं है और इंकार है', 'मालिक काबिज दखील संक्रमणीय भूमिधर', 'नजरी नक्शा', 'चौहद्दी', 'कहा-कहलवाया व समझाया किन्तु प्रतिवादीगण इंकारी हैं') from [DAD ORIGINAL DRAFT 1] and CLEAN_STYLE_ANCHOR.",
                "Replace all historical case details with the current case details from CASE_FACTS.",
            ]
        else:
            mode_rules = [
                f"DRAFTING STYLE MODE: HYBRID MIX (Template Structure + Dad's Language: {selected_tpl}).",
                "Combine the complete structural clauses and comprehensive prayer of SECTION 1 with Advocate B.D. Shukla's authentic court vocabulary and transition style ('यह कि ...') from SECTION 2.",
            ]

        package: dict[str, Any] = {
            "DOCUMENT_TYPE": label,
            "SELECTED_STYLE_MODE": STYLE_NAMES.get(style_mode, style_mode),
            "SELECTED_TEMPLATE_FILE": selected_tpl,
            "CASE_FACTS": state.facts,
            "CORPUS_REFERENCE": context,
            "CLEAN_STYLE_ANCHOR": style_anchor if style_mode != "template_mode" else "",
            "SYSTEM_RULES": BASE_RULES + mode_rules + [
                "Never output legacy-font artifacts or mixed Latin characters inside Hindi words.",
                "Use standard Unicode Devanagari Hindi in every Hindi sentence.",
            ],
            "OUTPUT_REQUIREMENTS": {
                "court_heading": "Court heading (e.g. न्यायालय श्रीमान सिविल जज (प्रवर खंड), औरैया).",
                "case_heading": "Formal suit/case number line ONLY (e.g. 'मूल वाद संख्या ............ सन् 2026 (दो हजार छब्बीस)'). Never put party names here.",
                "parties": "Each individual plaintiff and each individual defendant as a SEPARATE string item without 'वादी:' or 'प्रतिवादी:' prefixes.",
                "title": "Formal underlined document title (without statutory Act/Section citations).",
                "opening_averment": "Formal opening address (e.g. 'श्रीमान जी,\\nवादी निम्न प्रकार सविनय निवेदन करता है :-' or for Written Statement: 'श्रीमान जी,\\nप्रतिवादीगण की ओर से वादपत्र का लिखित कथन / जवाब दावा निम्न प्रकार सविनय प्रस्तुत है :-').",
                "pleadings": "Complete, richly elaborated factual paragraphs starting with 'यह कि'; for Written Statement, Paragraph 1 MUST reply to 'वादपत्र की धारा 1 (एक)', Paragraph 2 to 'वादपत्र की धारा 2 (दो)', etc., in exact 1-to-1 sequence with ZERO standalone section headings and ZERO statutory Act/Section citations; no numbering.",
                "prayer": "Comprehensive, multi-clause reliefs ((क), (ख), (ग), (घ)); do not repeat 'अतः प्रार्थना है कि' inside individual items.",
                "verification": "Formal paragraph-split verification body text WITHOUT the heading word 'सत्यापन' (in the name of Defendants for Written Statement, or Plaintiff for Plaint).",
                "signature_block": "Formal place, date, party signature line(s), and advocate signature block.",
                "layout": "Optional page-layout metadata.",
            },
        }
        if prior_plaint_paragraphs and state.document_type in {"written_statement", "affidavit"}:
            package["PLAINT_PARAGRAPHS_TO_REBUT"] = prior_plaint_paragraphs

        system = (
            style
            + "\n\nYou are the FINAL DRAFTING ENGINE for Indian Civil Court drafting.\n"
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
