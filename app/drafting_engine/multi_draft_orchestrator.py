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
    "MANDATORY BILINGUAL NUMBER FORMAT: Every numerical value (Gata/Khasra/Bhoomi number, area in hectare/biswa/bigha/decimal, age, date, time, money amount, fraction, and paragraph reference) MUST be written in digits followed by its Hindi words in parentheses, e.g., 'गाटा संख्या 412 (चार सौ बारह)', 'रकबा 0.250 (शून्य दशमलव दो पाँच शून्य) हेक्टेयर', '2 (दो) बिस्वा', 'उम्र लगभग 52 (बावन) वर्ष', 'दिनांक 12 अक्टूबर 2026 (बारह अक्टूबर दो हजार छब्बीस)', 'प्रातः 10:00 (दस) बजे', 'रु० 25,000/- (पच्चीस हजार)'. Never write bare number words like 'दो बिस्वा' without the digit '2 (दो) बिस्वा'.",
    "NEVER write thin, 1-sentence summaries. Every pleading paragraph must be written in rich, authoritative, multi-sentence court Hindi ('यह कि ...') matching the depth and professional legal articulation of the selected Master Template or Advocate B.D. Shukla's original court drafts.",
    "MANDATORY LEGAL COMPLETENESS FOR DAVA (PLAINT): Always include full paragraphs covering: (1) Ownership, Title & Revenue Record status, (2) Full Property Description, Chauhaddi/Boundaries & Site Plan reference, (3) Peaceful Possession & Usage, (4) Defendants' Lack of Right & Unlawful/Forceful Conduct ('जिसकी लाठी भैंस उसी की' वाला सूत्र अपनाकर और कानून के शासन को तोड़कर), (5) Specific Incident & Continuing Threat, (6) Repeated Requests & Defendants' Refusal (कहा-कहलवाया व समझाया किन्तु प्रतिवादीगण इंकारी हैं), (7) Formal Cause of Action ('वाद हेतुक' प्रथम बार दिनांक ... को उत्पन्न हुआ तथा निरन्तर जारी है), (8) Territorial & Pecuniary Jurisdiction (यह मान्य न्यायालय वाद की सुनवाई करने हेतु पूर्ण कार्य क्षेत्र/क्षेत्राधिकार रखती है), and (9) Valuation & Court Fee (न्याय शुल्क अधिनियम के प्राविधान के अनुसार उचित न्याय शुल्क पृथक से अदा की गई है).",
    "MANDATORY STATUTORY HEAVY-LIFTING FOR WRITTEN STATEMENT (जवाब दावा): When drafting a Written Statement, systematically rebut every paragraph of the Plaint and explicitly plead all applicable statutory bars and sections (धाराएं): धारा 41(एच), 41(आई), 41(जे) एवं धारा 34, 38, 39 विशिष्ट अनुतोष अधिनियम, 1963; आदेश 7 नियम 3, नियम 11(क) व (घ) एवं नियम 14 सिविल प्रक्रिया संहिता; धारा 24, 134, 144 सपठित धारा 206 उत्तर प्रदेश राजस्व संहिता, 2006 एवं धारा 9 सिविल प्रक्रिया संहिता (for Gata/agricultural land); आदेश 1 नियम 9 व 10 सिविल प्रक्रिया संहिता (आवश्यक पक्षकारों के असंयोजन व कुसंयोजन का दोष); धारा 35, धारा 101 लगायत 103 एवं धारा 115 भारतीय साक्ष्य अधिनियम (विबंधन एवं मौन सहमति का सिद्धांत); धारा 3 भारतीय परिसीमा अधिनियम, 1963; वाद मूल्यांकन अधिनियम एवं धारा 7(iv-ख) उत्तर प्रदेश न्यायालय शुल्क अधिनियम, 1870; तथा धारा 35-ए सिविल प्रक्रिया संहिता के अंतर्गत रु० 25,000/- (पच्चीस हजार) विशेष हर्जाना।",
    "Preserve event status exactly: attempt is not completion; threat is not dispossession; allegation is not an established fact.",
    "If CASE_FACTS contains custom_add_points, include every requested point as a proper legal paragraph/clause.",
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


def _format_cause_title_parties(parties: list[str]) -> list[str]:
    from .renderers.legal_document_renderer import split_party_blocks
    pl, df, other = split_party_blocks(parties)
    if not pl and not df:
        return parties
    out: list[str] = []
    role_re = r"\s*(?:[-–—.]+\s*)?(?:प्रतिवादीगण|प्रतिवादिनीगण|प्रतिवादिनी|प्रतिवादी|विपक्षीगण|विपक्षी|वादीगण|वादिनी|वादी|प्रार्थीगण|प्रार्थिनी|प्रार्थी)(?:\s*(?:संख्या|सं०|सं\.|नं०|नं\.)\s*\d+(?:\s*\([^)]+\))?)?\s*$"
    if pl:
        cleaned_pl = [re.sub(role_re, "", p).strip() for p in pl if re.sub(role_re, "", p).strip()]
        if len(cleaned_pl) > 1:
            cleaned_pl = [p if re.match(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]", p) else f"{i}. {p}" for i, p in enumerate(cleaned_pl, 1)]
            out.extend(cleaned_pl)
            out.append(".................... वादीगण")
        elif cleaned_pl:
            out.extend(cleaned_pl)
            out.append(".................... वादी")
    if df:
        cleaned_df = [re.sub(role_re, "", d).strip() for d in df if re.sub(role_re, "", d).strip()]
        if len(cleaned_df) > 1:
            cleaned_df = [d if re.match(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]", d) else f"{i}. {d}" for i, d in enumerate(cleaned_df, 1)]
            out.extend(cleaned_df)
            out.append(".................... प्रतिवादीगण")
        elif cleaned_df:
            out.extend(cleaned_df)
            out.append(".................... प्रतिवादी")
    out.extend(other)
    return out


def _clean_short_name(raw: str) -> str:
    m = re.split(r"\s+(?:पुत्र|पत्नी|पुत्री|विधवा|उम्र|निवासी)\b|,", str(raw or "").strip(), maxsplit=1)
    if m and m[0].strip():
        return re.sub(r"^\s*\d+(?:\s*\([^)]+\))?\s*[.)\-:]\s*", "", m[0].strip())
    return str(raw or "").strip()


def _extract_party_names_for_doc(facts: dict[str, Any], document_type: str = "dava_plaint") -> tuple[str, str]:
    """Return (short_party_names_str, role_label_str) appropriate for the document_type."""
    if document_type == "written_statement":
        defs = facts.get("defendants") or []
        if isinstance(defs, str):
            defs = [defs]
        names = [_clean_short_name(d) for d in defs if _clean_short_name(d)]
        if len(names) > 1:
            return " एवं ".join(names), "उपरोक्त प्रतिवादीगण"
        if len(names) == 1:
            return names[0], "उपरोक्त प्रतिवादी"
        return "प्रतिवादीगण", "उपरोक्त प्रतिवादीगण"

    for key in ("plaintiffs", "deponent", "sender", "parties"):
        val = facts.get(key)
        if isinstance(val, list) and val:
            names = [_clean_short_name(x) for x in val if _clean_short_name(x)]
            if names:
                role = "उपरोक्त वादीगण" if len(names) > 1 else "उपरोक्त वादी"
                return " एवं ".join(names), role
        elif isinstance(val, str) and val.strip():
            return _clean_short_name(val), "उपरोक्त वादी"
    return "वादी", "उपरोक्त वादी"


def _ensure_written_statement_statutory_arsenal(pleadings: list[str], facts: dict[str, Any]) -> list[str]:
    """Guarantee that a Written Statement (Jawab Dava) includes comprehensive statutory bars & sections."""
    cleaned_pleadings: list[str] = []
    for p in pleadings:
        s = re.sub(r"\bvexatious\b", "कपटपूर्ण एवं तंग करने वाले", str(p), flags=re.I)
        s = re.sub(r"\bfrivolous\b", "निराधार एवं तुच्छ", s, flags=re.I)
        s = re.sub(r"\bmaintainable\b", "पोषणीय", s, flags=re.I)
        cleaned_pleadings.append(s)

    joined = "\n".join(cleaned_pleadings)
    all_facts_text = json.dumps(facts, ensure_ascii=False)
    is_agri_or_gata = any(
        k in joined or k in all_facts_text
        for k in ("गाटा", "खसरा", "भूमिधर", "खतौनी", "कृषि", "बिस्वा", "हेक्टेयर", "चकमार्ग", "मेड़")
    )

    # 1. Specific Relief Act Sections 41(h), 41(i), 41(j) & Sections 34, 38, 39
    if "धारा 41" not in joined and "41(" not in joined:
        cleaned_pleadings.append(
            "यह कि विशेष वैधानिक आपत्ति (अंतर्गत धारा 34, 38, 39 एवं धारा 41(एच), 41(आई) व 41(जे) विशिष्ट अनुतोष अधिनियम, 1963) : "
            "वादी द्वारा प्रस्तुत वाद विशिष्ट अनुतोष अधिनियम, 1963 की धारा 41(एच), 41(आई) एवं 41(जे) सपठित धारा 34 व 38 के आज्ञापक प्रावधानों से पूर्णतः बाधित है। "
            "जहाँ कथित विवादित भूमि के स्वत्व, क्षेत्रफल, सीमांकन और आधिपत्य पर ही गंभीर विवाद हो, वहाँ बिना स्वत्व की उद्घोषणा (धारा 34) एवं सक्षम सीमांकन के मात्र स्थायी अथवा आदेशात्मक निषेधाज्ञा का वाद पोषणीय नहीं है। "
            "इसके अतिरिक्त वादी को सक्षम न्यायालय/अधिकारी के समक्ष समान रूप से प्रभावकारी वैकल्पिक उपचार (धारा 41(एच)) उपलब्ध है, वादी स्वच्छ व निर्मल हाथों से माननीय न्यायालय के समक्ष उपस्थित नहीं हुआ है (धारा 41(आई)), तथा कथित विवादित अंश पर वादी का कोई व्यक्तिगत विधिक अधिकार या स्थापित कब्जा नहीं है (धारा 41(जे))।"
        )

    # 2. Order 7 Rule 3, Order 7 Rule 11(a) & (d), and Order 7 Rule 14 CPC
    if "आदेश 7 नियम 3" not in joined or "आदेश 7 नियम 11" not in joined:
        cleaned_pleadings.append(
            "यह कि वैधानिक आपत्ति (अंतर्गत आदेश 7 नियम 3, आदेश 7 नियम 11(क) व (घ) एवं आदेश 7 नियम 14 सिविल प्रक्रिया संहिता, 1908) : "
            "वादी ने वादपत्र में कथित विवादित भूमि की सटीक भुजाओं, लंबाई-चौड़ाई, सर्वे नंबर के विशिष्ट सीमांकन तथा प्रमाणित पैमाना नक्शे का उल्लेख नहीं किया है, जिससे वाद आदेश 7 नियम 3 सिविल प्रक्रिया संहिता के आज्ञापक प्रावधानों के विपरीत होने के कारण निरस्त होने योग्य है। "
            "साथ ही वादपत्र के कथनों से प्रतिवादीगण के विरुद्ध कोई वास्तविक व वैध वाद हेतुक प्रकट नहीं होता है और वाद विधि द्वारा स्पष्ट रूप से बाधित होने के कारण आदेश 7 नियम 11(क) एवं 11(घ) सिविल प्रक्रिया संहिता के अंतर्गत प्रारंभिक स्तर पर ही खारिज किए जाने योग्य है।"
        )

    # 3. U.P. Revenue Code, 2006 Sections 24, 134, 144 read with Section 206 & Section 9 CPC
    if is_agri_or_gata and "धारा 206" not in joined:
        cleaned_pleadings.append(
            "यह कि क्षेत्राधिकार संबंधी वैधानिक आपत्ति (अंतर्गत धारा 24, 134 एवं 144 सपठित धारा 206 उत्तर प्रदेश राजस्व संहिता, 2006 एवं धारा 9 सिविल प्रक्रिया संहिता, 1908) : "
            "वादग्रस्त आराजी कृषि/राजस्व गाटा भूमि से संबंधित है तथा कथित मेड़ तोड़ने, रकबा कम होने अथवा सीमा विवाद (पैमाइश/मेड़बंदी) के निर्धारण का अनन्य क्षेत्राधिकार उत्तर प्रदेश राजस्व संहिता, 2006 की धारा 24 एवं धारा 134/144 के अंतर्गत केवल सक्षम राजस्व न्यायालय (उपजिलाधिकारी) को प्राप्त है। "
            "उत्तर प्रदेश राजस्व संहिता, 2006 की धारा 206 सपठित धारा 9 सिविल प्रक्रिया संहिता के अंतर्गत ऐसे राजस्व एवं सीमांकन विवादों के संबंध में दीवानी न्यायालय (सिविल कोर्ट) का क्षेत्राधिकार स्पष्ट रूप से वर्जित एवं बाधित है।"
        )

    # 4. Non-Joinder / Mis-Joinder (Order 1 Rule 9 & 10 CPC), Evidence Act (Sections 101-103, 115), and Limitation Act (Section 3)
    if "आदेश 1 नियम 9" not in joined and "धारा 115" not in joined:
        cleaned_pleadings.append(
            "यह कि वैधानिक आपत्ति (अंतर्गत आदेश 1 नियम 9 व 10 सिविल प्रक्रिया संहिता, धारा 101 लगायत 103 एवं धारा 115 भारतीय साक्ष्य अधिनियम तथा धारा 3 भारतीय परिसीमा अधिनियम, 1963) : "
            "वादी का वाद आवश्यक पक्षकारों (यथा ग्राम सभा, उत्तर प्रदेश सरकार एवं संबंधित सहखातेदारों) को पक्षकार न बनाए जाने के कारण आदेश 1 नियम 9 के परंतुक एवं नियम 10 सिविल प्रक्रिया संहिता के अंतर्गत असंयोजन एवं कुसंयोजन के दोष से ग्रस्त है। "
            "इसके अतिरिक्त दावा वादी भारतीय साक्ष्य अधिनियम की धारा 115 (धारा 121 भारतीय साक्ष्य अधिनियम, 2023) के अंतर्गत विबंधन, परित्याग एवं मौन सहमति के सिद्धांत से तथा भारतीय परिसीमा अधिनियम, 1963 की धारा 3 के अंतर्गत कालबाधित होने के कारण भी निरस्त होने योग्य है।"
        )

    # 5. Suit Valuation Act, 1887 & U.P. Court Fees Act, 1870 + Section 35-A CPC Special Costs
    if "35ए" not in joined and "35-ए" not in joined and "35अ" not in joined:
        cleaned_pleadings.append(
            "यह कि वादी ने वाद मूल्यांकन अधिनियम, 1887 एवं उत्तर प्रदेश न्यायालय शुल्क अधिनियम, 1870 की धारा 7(iv-ख) के प्रावधानों के विपरीत वाद का मूल्यांकन जानबूझकर अत्यंत कम किया है और अपर्याप्त न्याय शुल्क अदा किया है। "
            "वादी ने प्रतिवादीगण को अनावश्यक रूप से हैरान व परेशान करने तथा नाजायज दबाव बनाने (पेशबंदी) की नीयत से यह मिथ्या, मनगढ़ंत व निराधार वाद प्रस्तुत किया है, इसलिए वादी का वाद सव्यय खारिज कर प्रतिवादीगण को सिविल प्रक्रिया संहिता की धारा 35-ए के अंतर्गत रु० 25,000/- (पच्चीस हजार) विशेष हर्जाना दिलाया जाना न्यायसंगत है।"
        )

    return cleaned_pleadings


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
            if style_mode == "template_mode":
                out["case_heading"] = f"वाद नंबर {case_no} सन् {case_yr}"
            else:
                out["case_heading"] = f"मूल वाद संख्या {case_no} सन् {case_yr}"

    if not out.get("parties"):
        fallback_parties: list[str] = []
        for idx, pl in enumerate(facts.get("plaintiffs") or [], 1):
            s = str(pl).strip()
            if s:
                prefix = f"{idx}. " if len(facts.get("plaintiffs") or []) > 1 and not re.match(r"^\s*\d+[.)\-:]", s) else ""
                suffix = "" if "वादी" in s else (" — वादीगण" if len(facts.get("plaintiffs") or []) > 1 else " — वादी")
                fallback_parties.append(f"{prefix}{s}{suffix}")
        for idx, df in enumerate(facts.get("defendants") or [], 1):
            s = str(df).strip()
            if s:
                prefix = f"{idx}. " if len(facts.get("defendants") or []) > 1 and not re.match(r"^\s*\d+[.)\-:]", s) else ""
                suffix = "" if "प्रतिवादी" in s or "विपक्षी" in s else (" — प्रतिवादीगण" if len(facts.get("defendants") or []) > 1 else " — प्रतिवादी")
                fallback_parties.append(f"{prefix}{s}{suffix}")
        if not fallback_parties and facts.get("parties"):
            fallback_parties = [str(x).strip() for x in facts["parties"] if str(x).strip()]
        if fallback_parties:
            out["parties"] = fallback_parties
    if out.get("parties") and document_type != "legal_notice":
        out["parties"] = _format_cause_title_parties(out["parties"])

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
                f"यह कि वाद का मूल्यांकन स्थायी निषेधाज्ञा एवं वांछित अनुतोष हेतु रु० {val_amt} निश्चित है और न्याय शुल्क अधिनियम के प्राविधान के अनुसार {fee_str}।"
            )

        out["pleadings"] = pleadings

    # Ensure comprehensive statutory bars & sections in Written Statement (जवाब दावा)
    if document_type == "written_statement" and out.get("pleadings"):
        out["pleadings"] = _ensure_written_statement_statutory_arsenal(list(out["pleadings"]), facts)

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
    place_str = str(facts.get("court_complex") or facts.get("place") or "बमुकाम कचहरी बिधूना, औरैया").strip()
    total_p = len(out.get("pleadings") or [])

    if ver:
        ver_lines = [
            ln.strip() for ln in ver.splitlines()
            if ln.strip() and not re.match(r"^[\s\-–—:*_]*सत्यापन[\s\-–—:*_]*$", ln.strip())
        ]
        ver = "\n".join(ver_lines).strip()

        # Check if a Written Statement verification mistakenly named the Plaintiff ("उपरोक्त वादी" / plaintiff name) or "वाद पत्र के पैरा"
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
            factual_end = max(1, total_p - 5) if document_type == "written_statement" else max(1, total_p - 3)
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

        # Extract any previously drafted Plaint (Vaad Patra) from state.documents so Written Statement / Affidavit can reference exact paragraphs
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
                "PRESERVE THE FULL PARAGRAPH ELABORATION, LEGAL PROSE, AND COMPLETE SECTION STRUCTURE OF THE TEMPLATE: never compress the template into short 1-sentence summaries.",
                "Write every numeric value in digits followed by its Hindi words in parentheses, e.g., 412 (चार सौ बारह), 0.250 (शून्य दशमलव दो पाँच शून्य), 2 (दो) बिस्वा, 52 (बावन) वर्ष.",
                "Adapt all comprehensive Prayer sub-clauses ((क), (ख), (ग), (घ)) and formal paragraph-split Verification from the template.",
            ]
        elif style_mode == "dad_style":
            mode_rules = [
                f"DRAFTING STYLE MODE: DAD'S 306 ORIGINAL FILES MODE (Selected Reference: {selected_tpl}).",
                "Replicate Advocate B.D. Shukla's complete court structure, rich multi-sentence elaboration, and exact vocabulary ('मालिक काबिज दखील संक्रमणीय भूमिधर', 'नजरी नक्शा', 'चौहद्दी', 'एकांतिक अध्यासन व अधिभोग', 'मेली साजिशी व दबंग किस्म के लोग', 'कहा-कहलवाया व समझाया किन्तु प्रतिवादीगण इंकारी हैं', 'उजरात मजीद / अतिरिक्त कथन', 'हस्व दफा 35अ जा०दी०') from [DAD ORIGINAL DRAFT 1] and CLEAN_STYLE_ANCHOR.",
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
                "case_heading": "Formal suit/case number line ONLY (e.g. 'वाद नंबर ............ सन् 2026 (दो हजार छब्बीस)' or 'मूल वाद संख्या ............ सन् 2026 (दो हजार छब्बीस)'). Never put party names here.",
                "parties": "Complete party descriptions for plaintiff(s) and defendant(s).",
                "title": "Formal underlined suit/document title.",
                "opening_averment": "Formal opening address (e.g. 'श्रीमान जी,\\nवादी निम्न प्रकार सविनय निवेदन करता है :-' or for Written Statement: 'श्रीमान जी,\\nप्रतिवादीगण की ओर से वादपत्र का लिखित कथन / जवाब दावा निम्न प्रकार सविनय प्रस्तुत है :-').",
                "pleadings": "Complete, richly elaborated substantive paragraphs starting with 'यह कि'; for Written Statement include systematic para-wise rebuttals AND comprehensive statutory objections citing exact sections (धारा 41(एच),(आई),(जे) व धारा 34, 38 विशिष्ट अनुतोष अधिनियम, आदेश 7 नियम 3, नियम 11(क),(घ) व नियम 14 सी०पी०सी०, धारा 24, 134, 144 सपठित धारा 206 उ०प्र० राजस्व संहिता एवं धारा 9 सी०पी०सी०, आदेश 1 नियम 9 व 10 सी०पी०सी०, धारा 101-103 व धारा 115 साक्ष्य अधिनियम, धारा 3 परिसीमा अधिनियम, तथा धारा 35-ए सी०पी०सी० विशेष हर्जाना); no numbering.",
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
