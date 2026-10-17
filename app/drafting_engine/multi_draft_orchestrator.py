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
    "NEVER write thin, 1-sentence summaries. Every pleading paragraph must be written in rich, authoritative, multi-sentence court Hindi ('यह कि ...') matching the depth and professional legal articulation of the selected Master Template or Advocate B.D. Shukla's original court drafts.",
    "MANDATORY LEGAL COMPLETENESS: For a Dava (Plaint), always include full paragraphs covering: (1) Ownership, Title & Revenue Record status, (2) Full Property Description, Chauhaddi/Boundaries & Site Plan reference, (3) Peaceful Possession & Usage, (4) Defendants' Lack of Right & Unlawful/Forceful Conduct ('जिसकी लाठी भैंस उसी की' वाला सूत्र अपनाकर और कानून के शासन को तोड़कर), (5) Specific Incident & Continuing Threat, (6) Repeated Requests & Defendants' Refusal (कहा-कहलवाया व समझाया किन्तु प्रतिवादीगण इंकारी हैं), (7) Formal Cause of Action ('वाद हेतुक' प्रथम बार दिनांक ... को उत्पन्न हुआ तथा निरन्तर जारी है), (8) Territorial & Pecuniary Jurisdiction (यह मान्य न्यायालय वाद की सुनवाई करने हेतु पूर्ण कार्य क्षेत्र/क्षेत्राधिकार रखती है), and (9) Valuation & Court Fee (न्याय शुल्क अधिनियम के प्राविधान के अनुसार उचित न्याय शुल्क पृथक से अदा की गई है).",
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
    role_re = r"\s*(?:[-–—.]+\s*)?(?:प्रतिवादीगण|प्रतिवादिनीगण|प्रतिवादिनी|प्रतिवादी|विपक्षीगण|विपक्षी|वादीगण|वादिनी|वादी|प्रार्थीगण|प्रार्थिनी|प्रार्थी)\s*$"
    if pl:
        cleaned_pl = [re.sub(role_re, "", p).strip() for p in pl if re.sub(role_re, "", p).strip()]
        if len(cleaned_pl) > 1:
            cleaned_pl = [p if re.match(r"^\s*\d+[.)\-:]", p) else f"{i}. {p}" for i, p in enumerate(cleaned_pl, 1)]
            out.extend(cleaned_pl)
            out.append(".................... वादीगण")
        elif cleaned_pl:
            out.extend(cleaned_pl)
            out.append(".................... वादी")
    if df:
        cleaned_df = [re.sub(role_re, "", d).strip() for d in df if re.sub(role_re, "", d).strip()]
        if len(cleaned_df) > 1:
            cleaned_df = [d if re.match(r"^\s*\d+[.)\-:]", d) else f"{i}. {d}" for i, d in enumerate(cleaned_df, 1)]
            out.extend(cleaned_df)
            out.append(".................... प्रतिवादीगण")
        elif cleaned_df:
            out.extend(cleaned_df)
            out.append(".................... प्रतिवादी")
    out.extend(other)
    return out


def _extract_primary_party_name(facts: dict[str, Any]) -> str:
    for key in ("plaintiffs", "deponent", "sender", "parties"):
        val = facts.get(key)
        if isinstance(val, list) and val:
            raw = str(val[0]).strip()
        elif isinstance(val, str) and val.strip():
            raw = val.strip()
        else:
            continue
        # Extract name before 'पुत्र' / 'पत्नी' / ',' / 'उम्र'
        m = re.split(r"\s+(?:पुत्र|पत्नी|पुत्री|विधवा|उम्र|निवासी)\b|,", raw, maxsplit=1)
        if m and m[0].strip():
            return re.sub(r"^\s*\d+[.)\-:]\s*", "", m[0].strip())
    return "वादी"


def _normalize_draft_structure(
    draft: dict[str, Any],
    facts: dict[str, Any],
    document_type: str = "dava_plaint",
    style_mode: str = "template_mode",
) -> dict[str, Any]:
    """Unwrap nested model payloads and ensure complete court-grade headings, clauses, prayer, and verification."""
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

    # Clean Prayer clauses: strip redundant lead-ins inside individual sub-clauses
    if out.get("prayer"):
        cleaned_prayer: list[str] = []
        for item in out["prayer"]:
            s = re.sub(
                r"^\s*(?:\([क-ज्ञa-zA-Z0-9]+\)\s*)?(?:अतः\s*)?(?:अत्यन्त\s*विनयपूर्वक\s*)?(?:माननीय\s*न्यायालय\s*से\s*)?(?:सादर\s*)?प्रार्थना\s*(?:की\s*जाती\s*)?है\s*कि\s*[:-]*\s*",
                "",
                str(item).strip(),
            ).strip()
            if s:
                cleaned_prayer.append(s)
        out["prayer"] = cleaned_prayer

    # Clean Verification: remove duplicate leading "सत्यापन" heading line and ensure paragraph-range specificity
    ver = str(out.get("verification") or "").strip()
    if ver:
        ver_lines = [
            ln.strip() for ln in ver.splitlines()
            if ln.strip() and not re.match(r"^[\s\-–—:*_]*सत्यापन[\s\-–—:*_]*$", ln.strip())
        ]
        ver = "\n".join(ver_lines).strip()
        total_p = len(out.get("pleadings") or [])
        if total_p >= 4 and "पैरा" not in ver and "धारा" not in ver:
            party_name = _extract_primary_party_name(facts)
            place_str = str(facts.get("court_complex") or facts.get("place") or "बमुकाम कचहरी बिधूना, औरैया").strip()
            factual_end = max(1, total_p - 3)
            legal_start = factual_end + 1
            ver = (
                f"मैं कि {party_name}, उपरोक्त वादी, आज दिनांक ............ को स्थान {place_str} पर यह सत्यापित करता हूँ कि "
                f"वाद पत्र के पैरा नं० 1 से {factual_end} तक के कथन मेरी निजी जानकारी में सत्य व सही हैं तथा पैरा नं० {legal_start} से {total_p} तक के कथन "
                f"कानूनी सलाह के आधार पर मेरे विश्वास में सत्य हैं। कोई भी तथ्य असत्य नहीं है और न ही कोई तथ्य छिपाया गया है।"
            )
        out["verification"] = ver

    adv_name = str(facts.get("advocate_name") or "वी०डी० शुक्ला, अधिवक्ता").strip()
    court_complex = str(facts.get("court_complex") or "बमुकाम कचहरी बिधूना, औरैया").strip()
    party_name = _extract_primary_party_name(facts)

    if not out.get("signature_block"):
        out["signature_block"] = [
            f"स्थान : {court_complex}",
            f"दिनांक : ............",
            f"वादी — {party_name}",
            f"द्वारा अधिवक्ता — {adv_name}",
        ]

    return out


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

        if style_mode == "template_mode":
            mode_rules = [
                f"DRAFTING STYLE MODE: PURCHASED 500+ LEGAL TEMPLATES MODE (Selected Master Template: {selected_tpl}).",
                "Use [MASTER TEMPLATE 1] from CORPUS_REFERENCE as the direct Master Template for this draft.",
                "PRESERVE THE FULL PARAGRAPH ELABORATION, LEGAL PROSE, AND COMPLETE SECTION STRUCTURE OF THE TEMPLATE: never compress the template into short 1-sentence summaries.",
                "Adapt all standard template paragraphs to CASE_FACTS, including Ownership & Possession, Property Description & Chauhaddi (हदुदर्वा/चौहद्दी), Defendants' Unlawful Conduct & Rule of Law ('जिसकी लाठी भैंस उसी की' वाला सूत्र अपनाकर और कानून के शासन को तोड़कर), Request & Refusal, Cause of Action ('वाद हेतुक' प्रथम बार दिनांक ... को उत्पन्न हुआ तथा निरन्तर जारी है), Jurisdiction (यह मान्य न्यायालय वाद की सुनवाई करने हेतु कार्य क्षेत्र रखती है), and Valuation & Court Fee (न्याय शुल्क अधिनियम के प्राविधान के अनुसार उचित न्याय शुल्क पृथक से अदा की गई है).",
                "Adapt all comprehensive Prayer sub-clauses ((क), (ख), (ग), (घ)) and formal paragraph-split Verification from the template.",
            ]
        elif style_mode == "dad_style":
            mode_rules = [
                f"DRAFTING STYLE MODE: DAD'S 306 ORIGINAL FILES MODE (Selected Reference: {selected_tpl}).",
                "Replicate Advocate B.D. Shukla's complete 10-to-13 paragraph court structure, rich multi-sentence elaboration, and exact vocabulary ('मालिक काबिज दखील संक्रमणीय भूमिधर', 'नजरी नक्शा', 'चौहद्दी', 'एकांतिक अध्यासन व अधिभोग', 'मेली साजिशी व दबंग किस्म के लोग', 'कहा-कहलवाया व समझाया किन्तु प्रतिवादीगण इंकारी हैं', 'वाद का व्यवहार कारण चन्द दर चन्द कब्जे की कोशिश ऐलानिया धमकी के बाद इंकार आखिरी... श्रीमानजी को वादश्रवण एवं निर्णयाधिकार प्राप्त है', 'वाद का मूल्यांकन... न्याय शुल्क निर्धारित नियमानुसार अदा किया गया है', 'गर्जे कि प्रतिवादीगण वादी को बेदखल करके स्वयं कब्जा आदि का प्रयास न करें न करावें') from [DAD ORIGINAL DRAFT 1] and CLEAN_STYLE_ANCHOR.",
                "Replace all historical case details with the current case details from CASE_FACTS.",
            ]
        else:
            mode_rules = [
                f"DRAFTING STYLE MODE: HYBRID MIX (Template Structure + Dad's Language: {selected_tpl}).",
                "Combine the complete structural clauses and comprehensive prayer of SECTION 1 with Advocate B.D. Shukla's authentic court vocabulary and transition style ('यह कि ...') from SECTION 2.",
            ]

        package = {
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
                "case_heading": "Formal suit/case number line ONLY (e.g. 'वाद नंबर ............ सन् 2026' or 'मूल वाद संख्या ............ सन् 2026'). Never put party names here.",
                "parties": "Complete party descriptions for plaintiff(s) and defendant(s).",
                "title": "Formal underlined suit/document title (e.g. 'स्थायी व आदेशात्मक निषेधाज्ञा हेतु वाद पत्र अंतर्गत धारा 38 विशिष्ट अनुतोष अधिनियम सपठित आदेश 7 नियम 1 सिविल प्रक्रिया संहिता').",
                "opening_averment": "Formal opening address (e.g. 'श्रीमान जी,\\nवादी निम्न प्रकार सविनय निवेदन करता है :-').",
                "pleadings": "Complete, richly elaborated substantive paragraphs (9 to 12 paragraphs for a Plaint) starting with 'यह कि', including Cause of Action, Jurisdiction, and Valuation/Court Fee; no numbering.",
                "prayer": "Comprehensive, multi-clause reliefs (at least 3 to 4 detailed clauses covering primary decree against defendants/agents/servants, costs of suit, and omnibus relief); do not repeat 'अतः प्रार्थना है कि' inside individual items.",
                "verification": "Formal paragraph-split verification body text WITHOUT the heading word 'सत्यापन'.",
                "signature_block": "Formal place, date, plaintiff signature line, and advocate signature block.",
                "layout": "Optional page-layout metadata.",
            },
        }
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
            draft = sanitize_draft_unicode(normalized_raw)
            errors = self.validate(draft, state.facts, state.document_type)
            if not errors:
                state.status = "ready"
                if style_mode == "dad_style":
                    return format_draft_numbers(draft)
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
