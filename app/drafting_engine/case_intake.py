"""High-reliability semantic legal-case intake using Gemini.

The intake layer is deliberately NOT keyword/label driven. It treats every user
message as legal evidence, classifies it by semantic role, re-audits the
accumulated case when required fields are missing, and keeps deterministic
safety nets for obvious labelled blocks and request/narrative separation.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .gemini_client import GeminiClient

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "intake_schema.json"

TYPE_GUIDANCE = {
    "dava_plaint": "Plaint/वाद: court, initiating party, opposite party, property, material facts/events, cause-of-action events and requested final/interim relief.",
    "written_statement": "Written statement/लिखित कथन: plaintiff/defendant, defendant-side narrative, admissions, denials, objections, alternative pleas, documents and counter-relief.",
    "application": "Application/प्रार्थना पत्र: forum, applicant/opposite parties, background, purpose and every requested prayer/interim relief.",
    "evidence_pw_affidavit": "Evidence/PW affidavit: court, witness/deponent, relationship, testimony facts, dates, places and exhibits/documents.",
    "affidavit": "Affidavit/शपथपत्र: deponent, purpose, sworn factual statements, dates/place and verification.",
    "legal_notice": "Legal notice/विधिक नोटिस: sender, recipient, chronology, breach/default, demand, deadline and documents.",
    "other_civil": "Other civil document: identify the natural parties, forum, factual narrative, purpose and requested outcome without forcing an unsuitable category.",
}

SYSTEM = """You are the semantic legal-intake engine for an Indian advocate's drafting system.

The user will NOT use consistent wording. They may write Hindi, English, Hinglish, legal drafting language,
colloquial descriptions, chronology, fragments, copied legal text, voice transcripts, OCR text, or a mixture.
Your task is to understand the LEGAL MEANING and place each supported statement into the correct structured field.

CRITICAL RULES
1. NEVER require headings or labels. A fact is a fact because of its meaning, not because it says 'Facts'.
2. Treat the entire supplied user history as evidence. A case may be distributed across many messages.
3. Extract from user-authored text only. Assistant questions are never evidence.
4. Preserve material names, dates, addresses, amounts, survey/gata/khasra numbers, quotations and event status.
5. Do not invent anything. Do not infer ownership, title, possession, statute, limitation, jurisdiction, valuation,
   court fee, documents, witnesses or relief merely because such details are common in legal pleadings.
6. Distinguish semantic roles carefully:
   - event/allegation/background -> facts;
   - legally operative events giving rise to the proceeding -> cause_of_action;
   - what the court/recipient is asked to grant/do -> reliefs or demands;
   - temporary order sought pending final disposal -> interim_reliefs;
   - denial/admission/objection/alternative plea -> defence_points;
   - evidence/documents mentioned -> documents.
7. Threat is not dispossession. Attempt is not completion. Allegation is not an established fact. Keep those statuses.
8. Recognize legal roles and expressions across languages and beyond any example vocabulary. Use context and grammar,
   not a fixed synonym list. For example, 'विपक्षी', 'सामने वाला', 'opposite party', 'respondent' can describe the
   adverse party depending on document type; 'अतः', 'इसलिए', 'न्यायहित में', 'prayer', 'I therefore request',
   'may kindly restrain', etc. can signal a requested outcome. These are examples, not a closed dictionary.
9. If a user provides a complete narrative without labels, reconstruct the structured fields from the narrative.
10. If the newest message corrects an earlier fact, prefer the corrected value and do not preserve the contradicted
    value merely because it was extracted earlier.
11. If a field is genuinely absent, return it empty. Never fill it with a typical legal assumption.
12. Return JSON only matching the supplied schema.
"""

AUDIT_SYSTEM = """You are the final completeness auditor for an Indian legal drafting intake.

You receive accumulated USER EVIDENCE, the CURRENT CASE STATE, and a first-pass semantic extraction.
Your job is to recover any supported information that the first pass missed or placed in the wrong field.

AUDIT RULES
- Read the user evidence semantically; do not search only for headings or exact keywords.
- Recover facts expressed indirectly, narratively, chronologically, conversationally, in Hindi, English,
  Hinglish, OCR text or copied legal prose.
- If a statement is clearly a factual event/background, put it in facts.
- If it states the legal event(s) giving rise to the proceeding, put those events in cause_of_action.
- If it asks a court/authority/person to do something, put that requested outcome in reliefs/demands as appropriate.
- Never confuse a request with the event that motivated it.
- Do not invent or strengthen allegations.
- Existing case facts are evidence, but user corrections supersede earlier values.
- Return only NEW or CORRECTED supported fields. Empty fields mean 'nothing additional found'.
"""

LABELS = {
    "court_name": ["Court", "न्यायालय", "अदालत", "कोर्ट"],
    "plaintiff_intro": ["Plaintiff", "वादी", "दावेदार", "Claimant"],
    "defendant_intro": ["Defendant", "प्रतिवादी", "विपक्षी", "Respondent", "Opposite Party"],
    "property_description": ["Property", "संपत्ति", "विवादित भूमि", "भूमि", "मकान"],
    "facts": ["Main Facts", "Facts", "मुख्य तथ्य", "तथ्य"],
    "cause_of_action": ["Cause of action", "वाद हेतुक", "वाद हेतु", "वाद हेतुक की तारीख"],
    "reliefs": ["Relief", "Reliefs", "राहत", "प्रार्थना", "अनुतोष", "Prayer"],
    "demands": ["Demand", "Demands", "मांग", "अनुपालन"],
    "defence_points": ["Defence", "प्रतिरक्षा", "बचाव", "आपत्ति"],
    "purpose": ["Purpose", "उद्देश्य"],
    "sender": ["Sender", "प्रेषक", "नोटिसदाता"],
    "recipient": ["Recipient", "प्राप्तकर्ता", "नोटिसी"],
    "deponent": ["Deponent", "शपथकर्ता"],
}

RELIEF_MARKERS = re.compile(
    r"(?:अतः|अत:|इसलिए|इस हेतु|न्यायहित में|प्रार्थना है|निवेदन है|राहत दी जाए|राहत प्रदान|"
    r"रोका जाए|रोक दिया जाए|घोषित किया जाए|कब्जा दिलाया जाए|आदेशित किया जाए|"
    r"may kindly|it is therefore prayed|prayer|relief|restrain|restrained|directed|declared|"
    r"grant|award|allow|dismiss|decree|sought|requested)", re.I
)


def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _dedupe(items: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("text") or item.get("content") or ""
        value = _clean(item)
        key = re.sub(r"\s+", " ", value).casefold()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _extract_labelled(message: str) -> dict[str, Any]:
    """Deterministic safety net for explicit Label: blocks; never the primary classifier."""
    text = message.replace("\r\n", "\n").replace("\r", "\n")
    variants: list[str] = []
    canonical: dict[str, str] = {}
    for key, vals in LABELS.items():
        for value in vals:
            variants.append(value)
            canonical[value.casefold()] = key
    alt = "|".join(sorted((re.escape(v) for v in variants), key=len, reverse=True))
    pattern = re.compile(
        rf"(?ims)(?<!\w)(?P<label>{alt})\s*:\s*(?P<value>.*?)(?=\n\s*(?:{alt})\s*:|\Z)"
    )
    out: dict[str, Any] = {}
    for match in pattern.finditer(text):
        key = canonical.get(match.group("label").strip().casefold())
        value = match.group("value").strip()
        if not key or not value:
            continue
        if key in {"facts", "reliefs", "demands", "defence_points"}:
            values = [
                re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", line).strip()
                for line in value.splitlines()
            ]
            out[key] = [x for x in values if x] or [value]
        else:
            out[key] = value
    if out.get("plaintiff_intro"):
        out["plaintiffs"] = [out["plaintiff_intro"]]
    if out.get("defendant_intro"):
        out["defendants"] = [out["defendant_intro"]]
    return out


def _narrative_rescue(message: str, result: dict[str, Any]) -> dict[str, Any]:
    """Last-resort rescue when Gemini returns an incomplete classification.

    It is intentionally conservative: it never fabricates parties/court/property. For missing
    facts it preserves the user's narrative; for missing relief it only extracts sentences that
    clearly contain request/outcome language.
    """
    out = dict(result or {})
    if not out.get("facts"):
        raw_parts = [p.strip() for p in re.split(r"(?<=[।.!?])\s+|\n+", message) if p.strip()]
        relief_parts = [p for p in raw_parts if RELIEF_MARKERS.search(p)]
        fact_parts = [p for p in raw_parts if p not in relief_parts]
        if fact_parts:
            out["facts"] = fact_parts
        elif raw_parts and not relief_parts:
            out["facts"] = raw_parts
    if not out.get("reliefs"):
        raw_parts = [p.strip() for p in re.split(r"(?<=[।.!?])\s+|\n+", message) if p.strip()]
        relief_parts = [p for p in raw_parts if RELIEF_MARKERS.search(p)]
        if relief_parts:
            out["reliefs"] = relief_parts
    return out


def _normalize(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result or {})
    for key in ("plaintiffs", "defendants", "parties", "facts", "reliefs", "interim_reliefs", "demands", "defence_points", "documents"):
        if key in result:
            result[key] = _dedupe(result.get(key) if isinstance(result.get(key), list) else [result.get(key)])
    if result.get("plaintiff_intro") and not result.get("plaintiffs"):
        result["plaintiffs"] = [_clean(result["plaintiff_intro"])]
    if result.get("defendant_intro") and not result.get("defendants"):
        result["defendants"] = [_clean(result["defendant_intro"])]
    if result.get("plaintiffs") or result.get("defendants"):
        result["parties"] = _dedupe(list(result.get("parties", [])) + list(result.get("plaintiffs", [])) + list(result.get("defendants", [])))
    return result


def _history_for_model(history: list[str], max_chars: int | None = None) -> list[str]:
    cleaned = [_clean(x) for x in history if _clean(x)]
    if not max_chars:
        return cleaned
    out: list[str] = []
    total = 0
    for item in reversed(cleaned):
        if total + len(item) > max_chars:
            break
        out.append(item)
        total += len(item)
    return list(reversed(out))


def _call(client: GeminiClient, system: str, payload: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    return client.generate_json(
        system=system,
        prompt=json.dumps(payload, ensure_ascii=False, indent=2),
        schema=schema,
        thinking_level=os.getenv("GEMINI_INTAKE_THINKING_LEVEL", "medium"),
    ) or {}


def extract_case_facts(
    message: str,
    current_facts: dict[str, Any] | None = None,
    document_type: str = "dava_plaint",
    user_history: list[str] | None = None,
) -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    history = [str(x) for x in (user_history or []) if str(x).strip()]
    if not history or history[-1].strip() != message.strip():
        history.append(message)

    # Keep enough history to recover facts split over messages while avoiding unbounded prompts.
    history = _history_for_model(history, int(os.getenv("GEMINI_INTAKE_HISTORY_CHARS", "80000")))
    current = _normalize(current_facts or {})
    client = GeminiClient()

    payload = {
        "document_type": document_type,
        "document_type_guidance": TYPE_GUIDANCE.get(document_type, TYPE_GUIDANCE["other_civil"]),
        "current_case_state": current,
        "user_messages_in_order": history,
        "newest_user_message": message,
        "task": "Extract every newly supported or corrected fact by legal meaning. Do not wait for labels. Return empty fields only when the evidence truly does not support them.",
    }

    first: dict[str, Any] = {}
    try:
        first = _call(client, SYSTEM, payload, schema)
    except Exception:
        first = {}
    first = _normalize(first)

    # Explicit labels are an override for that exact labelled field, not the general classifier.
    labelled = _normalize(_extract_labelled(message))
    candidate = _normalize({**first, **labelled})

    # If a required field is still missing, perform a second independent audit against the accumulated evidence.
    # This is the key recovery mechanism for long, naturally worded cases and OCR transcripts.
    required = {
        "dava_plaint": ("court_name", "plaintiffs", "defendants", "facts", "reliefs"),
        "written_statement": ("court_name", "plaintiffs", "defendants", "facts"),
        "application": ("court_name", "parties", "facts", "reliefs"),
        "evidence_pw_affidavit": ("court_name", "deponent", "facts"),
        "affidavit": ("deponent", "facts", "purpose"),
        "legal_notice": ("sender", "recipient", "facts", "demands"),
        "other_civil": ("parties", "facts", "purpose"),
    }.get(document_type, ("parties", "facts", "purpose"))
    missing = [key for key in required if not candidate.get(key) and not current.get(key)]

    audit: dict[str, Any] = {}
    if missing or os.getenv("GEMINI_INTAKE_AUDIT_ALWAYS", "1").strip().lower() in {"1", "true", "yes", "on"}:
        audit_payload = {
            "document_type": document_type,
            "required_fields_still_missing": missing,
            "current_case_state": current,
            "first_pass_extraction": candidate,
            "user_messages_in_order": history,
            "newest_user_message": message,
            "task": "Audit the complete user evidence. Recover omitted or misclassified supported information, especially facts and requested relief. Return only new/corrected fields.",
        }
        try:
            audit = _normalize(_call(client, AUDIT_SYSTEM, audit_payload, schema))
        except Exception:
            audit = {}

    merged = _normalize(candidate)
    # Merge audit information without throwing away already captured facts.
    for key, value in audit.items():
        if value in (None, "", [], {}):
            continue
        if key in {"plaintiffs", "defendants", "parties", "facts", "reliefs", "interim_reliefs", "demands", "defence_points", "documents"}:
            merged[key] = _dedupe(list(merged.get(key, [])) + (value if isinstance(value, list) else [value]))
        else:
            merged[key] = value

    # Final deterministic semantic rescue for the two fields that caused the reported failure.
    merged = _normalize(_narrative_rescue(message, merged))
    return merged


def reconcile_case_state(
    current_facts: dict[str, Any] | None,
    document_type: str,
    user_history: list[str],
) -> dict[str, Any]:
    """Re-run semantic extraction over the accumulated user evidence.

    Used immediately before Generate Draft so a case collected by an older
    intake pass can recover facts that were previously missed.
    """
    history = [str(x) for x in (user_history or []) if str(x).strip()]
    if not history:
        return _normalize(current_facts or {})
    return extract_case_facts(
        history[-1],
        current_facts=current_facts or {},
        document_type=document_type,
        user_history=history,
    )
