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
    "CASE_FACTS are the sole authority for case-specific facts.",
    "Never invent names, dates, addresses, property identifiers, statutes, case law, ownership, title, possession, witnesses, valuation, court fee, limitation or jurisdiction.",
    "Preserve event status exactly: attempt is not completion; threat is not dispossession; allegation is not an established fact.",
    "If CASE_FACTS contains custom_add_points, include every requested point as a proper legal paragraph/clause.",
    "If CASE_FACTS contains custom_remove_points, omit those topics/clauses from the draft.",
    "Never number paragraphs yourself. The deterministic renderer assigns continuous Hindi numbering.",
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


def _normalize_draft_structure(draft: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Unwrap nested model payloads and ensure required list/scalar fields are properly populated."""
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

    if not out.get("parties"):
        fallback_parties: list[str] = []
        for pl in (facts.get("plaintiffs") or []):
            s = str(pl).strip()
            if s:
                fallback_parties.append(s if "वादी" in s else f"{s} --- वादी")
        for idx, df in enumerate(facts.get("defendants") or [], 1):
            s = str(df).strip()
            if s:
                prefix = "" if re.match(r"^\s*\d+[.)\-:]", s) else f"{idx}. "
                suffix = "" if "प्रतिवादी" in s or "विपक्षी" in s else (" --- प्रतिवादीगण" if idx == len(facts.get("defendants") or []) else "")
                fallback_parties.append(f"{prefix}{s}{suffix}")
        if not fallback_parties and facts.get("parties"):
            fallback_parties = [str(x).strip() for x in facts["parties"] if str(x).strip()]
        if fallback_parties:
            out["parties"] = fallback_parties

    adv_name = str(facts.get("advocate_name") or "वी०डी० शुक्ला एडवोकेट").strip()
    court_complex = str(facts.get("court_complex") or "सिविल कोर्ट कम्पाउण्ड, बिधूना, औरैया").strip()

    if not out.get("signature_block"):
        out["signature_block"] = ["वादी / प्रार्थी", "द्वारा अधिवक्ता-", adv_name, court_complex]

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
                f"DRAFTING STYLE MODE: PURCHASED 500+ LEGAL TEMPLATES MODE (Selected Template: {selected_tpl}).",
                "Use [MASTER TEMPLATE 1] from CORPUS_REFERENCE as the direct template for this draft.",
                "Keep the template's exact structure, legal clauses, statutory provisions, headings, and professional flow.",
                "Replace all placeholder blanks and sample details with the user's current case details from CASE_FACTS (Court Name, Court Complex, Parties' Names, Issue/Facts, Property, Reliefs, and Advocate Name).",
            ]
        elif style_mode == "dad_style":
            mode_rules = [
                f"DRAFTING STYLE MODE: DAD'S 306 ORIGINAL FILES MODE (Selected Reference: {selected_tpl}).",
                "Replicate Advocate B.D. Shukla's exact court drafting language, vocabulary ('मालिक काबिज दखील', 'मुकद्दमा हाजा के वाकयात', 'शराफतन'), paragraph rhythm ('यह कि ...'), and structure from [DAD ORIGINAL DRAFT 1] and CLEAN_STYLE_ANCHOR.",
                "Replace all historical case details with the current case details from CASE_FACTS.",
            ]
        else:
            mode_rules = [
                f"DRAFTING STYLE MODE: HYBRID MIX (Template Structure + Dad's Language: {selected_tpl}).",
                "Prioritize the professional template in SECTION 1 for structural headings, statutory grounds, cause of action, jurisdiction, valuation, and prayer points.",
                "Infuse Advocate B.D. Shukla's authentic court vocabulary and transition style ('यह कि ...') from SECTION 2.",
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
                "court_heading": "Court heading if supplied/applicable; otherwise empty.",
                "case_heading": "Party/case heading using only supplied facts.",
                "parties": "Party lines only; do not invent.",
                "title": "Correct document title matching the selected template and case.",
                "opening_averment": "Opening paragraph/statement appropriate to the document type.",
                "pleadings": "Ordered substantive paragraphs starting with 'यह कि' where natural; no numbering.",
                "prayer": "Only supported reliefs/demands, empty when inappropriate.",
                "verification": "Only when appropriate and grounded in supplied facts.",
                "signature_block": "Use supplied signer/deponent information and advocate_name / court_complex from CASE_FACTS.",
                "layout": "Optional page-layout metadata. Use only when explicitly requested.",
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
            normalized_raw = _normalize_draft_structure(raw_draft, state.facts)
            draft = sanitize_draft_unicode(normalized_raw)
            errors = self.validate(draft, state.facts, state.document_type)
            if not errors:
                state.status = "ready"
                return format_draft_numbers(draft)
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
