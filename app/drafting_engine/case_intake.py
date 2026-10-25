"""High-reliability semantic legal-case intake using Azure AI / Gemini.

The intake layer is deliberately NOT keyword/label driven. It treats every user
message as legal evidence, classifies it by semantic role, re-audits the
accumulated case when required fields are missing, and keeps deterministic
safety nets for obvious labelled blocks, long structured documents, and request/narrative separation.
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
    "dava_plaint": "Plaint/वाद: court, court complex, advocate name, initiating party, opposite party, property, material facts/events, cause-of-action events and requested final/interim relief.",
    "written_statement": "Written statement/लिखित कथन: plaintiff/defendant, defendant-side narrative, admissions, denials, objections, alternative pleas, documents and counter-relief.",
    "application": "Application/प्रार्थना पत्र व आपत्ति पत्र: forum, applicant/opposite parties, background, purpose and every requested prayer/interim relief.",
    "evidence_pw_affidavit": "Evidence/PW affidavit: court, witness/deponent, relationship, testimony facts, dates, places and exhibits/documents.",
    "affidavit": "Affidavit/शपथपत्र (any judicial, administrative, university, scholarship, revenue, or notary affidavit): authority/forum (समक्ष/न्यायालय), deponent (शपथकर्ता/शपथग्रहीता full details), purpose/subject, all sworn factual statements (प्रस्तर/धारा 1..N), place, date, advocate/registration details, and verification.",
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
4. Preserve material names, dates, addresses, amounts, survey/gata/khasra numbers, roll/registration numbers, quotations, court complex/authority (समक्ष), and advocate name.
5. Do not invent anything. Do not infer ownership, title, possession, statute, limitation, jurisdiction, valuation,
   court fee, documents, witnesses or relief merely because such details are common in legal pleadings.
6. Distinguish semantic roles carefully:
   - event/allegation/background/sworn statement -> facts;
   - legally operative events giving rise to the proceeding -> cause_of_action;
   - what the court/authority/recipient is asked to grant/do -> reliefs or demands;
   - temporary order sought pending final disposal -> interim_reliefs;
   - denial/admission/objection/alternative plea -> defence_points;
   - evidence/documents mentioned -> documents;
   - explicit instructions to add an extra point/clause -> custom_add_points;
   - explicit instructions to omit/remove a point/clause from the template -> custom_remove_points;
   - for Affidavit (शपथपत्र), extract the person making the oath into `deponent`, the authority/court (`समक्ष:` or `न्यायालय:`) into `court_name`, the subject/goal into `purpose`, and each sworn point into `facts`.
7. Threat is not dispossession. Attempt is not completion. Allegation is not an established fact. Keep those statuses.
8. Recognize legal roles and expressions across languages and beyond any example vocabulary.
9. If a user provides a complete narrative without labels, reconstruct the structured fields from the narrative.
10. If the newest message corrects an earlier fact, prefer the corrected value.
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
- If a statement is clearly a factual event/background or sworn affidavit clause, put it in facts.
- If it states the legal event(s) giving rise to the proceeding, put those events in cause_of_action.
- If it asks a court/authority/person to do something, put that requested outcome in reliefs/demands as appropriate.
- For an Affidavit (शपथपत्र), ensure `deponent` (शपथकर्ता/शपथग्रहीता), `purpose` (विषय/उद्देश्य), `court_name` (समक्ष/न्यायालय), and `facts` are populated whenever present in the user text.
- Extract advocate_name and court_complex if mentioned anywhere by the user.
- Extract custom_add_points and custom_remove_points if the user asked to add or remove specific points.
- Never confuse a request with the event that motivated it.
- Do not invent or strengthen allegations.
- Existing case facts are evidence, but user corrections supersede earlier values.
- Return only NEW or CORRECTED supported fields. Empty fields mean 'nothing additional found'.
"""

LABELS = {
    "court_name": ["Court", "न्यायालय", "अदालत", "कोर्ट", "समक्ष", "Before"],
    "court_complex": ["Court Complex", "कचहरी", "न्यायालय परिसर", "कोर्ट कम्पाउण्ड"],
    "advocate_name": ["Advocate", "Advocate Name", "अधिवक्ता", "वकील", "एडवोकेट", "Counsel", "द्वारा अधिवक्ता"],
    "plaintiff_intro": ["Plaintiff", "वादी", "दावेदार", "Claimant", "प्रार्थीगण", "आवेदकगण"],
    "defendant_intro": ["Defendant", "प्रतिवादी", "विपक्षी", "Respondent", "Opposite Party", "आपत्तिकर्त्री", "प्रतिवादिनी"],
    "property_description": ["Property", "संपत्ति", "विवादित भूमि", "भूमि", "मकान", "विवादित संपत्ति"],
    "facts": ["Main Facts", "Facts", "मुख्य तथ्य", "तथ्य", "विवरण", "Issue", "Case Details", "शपथपत्र के कथन"],
    "cause_of_action": ["Cause of action", "वाद हेतुक", "वाद हेतु", "वाद हेतुक की तारीख", "कारण वाद"],
    "reliefs": ["Relief", "Reliefs", "राहत", "प्रार्थना", "अनुतोष", "Prayer", "मांगी गई राहत", "अनुतोष की मांग", "दावाकृत अनुतोष"],
    "demands": ["Demand", "Demands", "मांग", "अनुपालन"],
    "defence_points": ["Defence", "प्रतिरक्षा", "बचाव", "आपत्ति"],
    "custom_add_points": ["Add Point", "Add Points", "अतिरिक्त बिंदु", "जोड़ने योग्य बिंदु"],
    "custom_remove_points": ["Remove Point", "Remove Points", "हटाने योग्य बिंदु"],
    "purpose": ["Purpose", "उद्देश्य", "विषय", "Subject"],
    "sender": ["Sender", "प्रेषक", "नोटिसदाता"],
    "recipient": ["Recipient", "प्राप्तकर्ता", "नोटिसी"],
    "deponent": ["Deponent", "शपथकर्ता", "शपथकर्त्री", "शपथग्रहीता", "शपथग्रहीत्री", "हस्ताक्षर शपथग्रहीता", "हस्ताक्षर शपथकर्ता"],
}

RELIEF_MARKERS = re.compile(
    r"(?:अतः|अत:|इसलिए|इस हेतु|न्यायहित में|प्रार्थना है|निवेदन है|राहत दी जाए|राहत प्रदान|"
    r"रोका जाए|रोक दिया जाए|घोषित किया जाए|कब्जा दिलाया जाए|आदेशित किया जाए|निरस्त करने की कृपा|"
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
        if key in {"facts", "reliefs", "demands", "defence_points", "custom_add_points", "custom_remove_points"}:
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


def _extract_long_structured_pleading_fast(message: str) -> dict[str, Any]:
    """Deterministically extract court, case_number, parties, facts (all 'यह कि' paragraphs), reliefs, and advocate
    from long pasted or PDF-extracted Hindi legal documents (e.g. 5 to 100 paragraphs)."""
    from .multi_draft_orchestrator import parse_structured_legal_document

    parsed = parse_structured_legal_document(message)
    pleadings = parsed.get("pleadings") or []
    if len(pleadings) < 2:
        return {}

    out: dict[str, Any] = {
        "facts": pleadings,
    }
    if parsed.get("court_heading"):
        out["court_name"] = parsed["court_heading"]
    if parsed.get("case_heading"):
        m_num = re.search(r"([0-9०-९]+\s*/\s*[0-9०-९]{4}|[0-9०-९]+)", parsed["case_heading"])
        if m_num:
            out["case_number"] = m_num.group(1).strip()
    parties = parsed.get("parties") or []
    if parties:
        if "बनाम" in parties:
            b_idx = parties.index("बनाम")
            pls = [p for p in parties[:b_idx] if p != "बनाम"]
            dfs = [p for p in parties[b_idx + 1:] if p != "बनाम"]
            if pls:
                out["plaintiffs"] = pls
            if dfs:
                out["defendants"] = dfs
        out["parties"] = [p for p in parties if p != "बनाम"]
    if parsed.get("prayer"):
        out["reliefs"] = parsed["prayer"]
    if parsed.get("title"):
        out["purpose"] = parsed["title"].splitlines()[0].strip()
    sig = "\n".join(parsed.get("signature_block") or [])
    m_adv = re.search(r"(?:द्वारा\s*अधिवक्ता\s*[:\-–—]?\s*)([^\n]+)", sig)
    if m_adv and m_adv.group(1).strip():
        out["advocate_name"] = m_adv.group(1).strip(" ()")
    else:
        for ln in parsed.get("signature_block") or []:
            if "शुक्ल" in ln or "एडवोकेट" in ln or "अधिवक्ता" in ln:
                if not ln.startswith("द्वारा"):
                    out["advocate_name"] = ln.strip(" ()")
                    break
    return out


def _narrative_rescue(message: str, result: dict[str, Any]) -> dict[str, Any]:
    """Last-resort rescue when AI returns an incomplete classification."""
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


def _rescue_affidavit_fields(message: str, result: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Deterministically recover authority (समक्ष/न्यायालय), deponent (शपथकर्ता/शपथग्रहीता), and purpose from any pasted or dictated Affidavit."""
    out = dict(result or {})
    text = str(message or "").strip()
    if not text:
        return out

    if not out.get("court_name") and not current.get("court_name"):
        m_samaksh = re.search(r"(?m)^\s*(?:समक्ष|न्यायालय)\s*[:\-–—]?\s*(.+)$", text)
        if m_samaksh:
            out["court_name"] = f"समक्ष: {_clean(m_samaksh.group(1))}" if "समक्ष" in m_samaksh.group(0) else _clean(m_samaksh.group(0))

    if not out.get("deponent") and not current.get("deponent"):
        m_main = re.search(
            r"मैं\s*,?\s*(?:शपथकर्ता|शपथकर्त्री|शपथग्रहीता|शपथग्रहीत्री)?\s*([^\n।]{2,160}?)(?=,?\s*शपथपूर्वक|\s*सत्यनिष्ठा|\n|।)",
            text,
        )
        if m_main and "सत्यापित करता" not in m_main.group(1):
            out["deponent"] = _clean(m_main.group(1))
        if not out.get("deponent"):
            m_sig = re.search(
                r"(?:हस्ताक्षर\s*)?(?:शपथग्रहीता|शपथग्रहीत्री|शपथकर्त्री|शपथकर्ता)\s*[:\-–—]?\s*\n?\s*\(?([^\n()]{2,80})\)?",
                text,
            )
            if m_sig:
                cand = _clean(m_sig.group(1))
                if cand and not any(w in cand for w in ("सत्यनिष्ठा", "सत्यापित", "उपरोक्त")):
                    out["deponent"] = cand
        if not out.get("deponent"):
            for fallback_key in ("plaintiffs", "parties", "sender"):
                vals = out.get(fallback_key) or current.get(fallback_key)
                if isinstance(vals, list) and vals:
                    out["deponent"] = _clean(vals[0])
                    break
                elif isinstance(vals, str) and vals.strip():
                    out["deponent"] = _clean(vals)
                    break

    if not out.get("advocate_name") and not current.get("advocate_name"):
        m_adv = re.search(
            r"(?:अधिवक्ता|एडवोकेट)[^\n]*\n\s*(?:पंजीकरण\s*सं[०.]?\s*[:\-]?\s*[A-Za-z0-9/\-]+)(?:\s*\n\s*मो[०.]?\s*[:\-]?\s*\d{10})?",
            text,
        )
        if m_adv:
            out["advocate_name"] = _clean(m_adv.group(0))

    has_facts = bool(out.get("facts") or current.get("facts"))
    if has_facts and not out.get("purpose") and not current.get("purpose"):
        m_subj = re.search(r"(?m)^\s*(?:विषय|उद्देश्य|बाबत|वास्ते)\s*[:\-–—]\s*(.+)$", text)
        if m_subj:
            out["purpose"] = _clean(m_subj.group(1))
        elif out.get("court_name") or current.get("court_name"):
            forum = out.get("court_name") or current.get("court_name")
            out["purpose"] = f"{forum} के समक्ष प्रस्तुत करने हेतु शपथपूर्वक घोषणा"
        else:
            out["purpose"] = "सक्षम प्राधिकारी / न्यायालय के समक्ष शपथपूर्वक कथन प्रस्तुत करने हेतु"

    return out


def _normalize(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result or {})
    for key in (
        "plaintiffs", "defendants", "parties", "facts", "reliefs", "interim_reliefs",
        "demands", "defence_points", "documents", "custom_add_points", "custom_remove_points"
    ):
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

    current = _normalize(current_facts or {})

    # Fast deterministic path for already-structured long pleadings/petitions (e.g. 5 to 100 'यह कि' paragraphs)
    # Prevents multi-call LLM timeouts when a user uploads a multi-page PDF or pastes a 17-paragraph document!
    combined_source = "\n\n".join(history)
    fast_structured = _normalize(_extract_long_structured_pleading_fast(combined_source))
    labelled = _normalize(_extract_labelled(message))

    required = {
        "dava_plaint": ("court_name", "plaintiffs", "defendants", "facts", "reliefs"),
        "written_statement": ("court_name", "plaintiffs", "defendants", "facts"),
        "application": ("court_name", "parties", "facts", "reliefs"),
        "evidence_pw_affidavit": ("court_name", "deponent", "facts"),
        "affidavit": ("deponent", "facts", "purpose"),
        "legal_notice": ("sender", "recipient", "facts", "demands"),
        "other_civil": ("parties", "facts", "purpose"),
    }.get(document_type, ("parties", "facts", "purpose"))

    if len(fast_structured.get("facts") or []) >= 3:
        fast_merged = _normalize({**current, **labelled, **fast_structured})
        if document_type in {"affidavit", "evidence_pw_affidavit"}:
            fast_merged = _normalize(_rescue_affidavit_fields(combined_source, fast_merged, current))
        fast_missing = [key for key in required if not fast_merged.get(key)]
        if not fast_missing or len(combined_source) >= 2200:
            return fast_merged

    history = _history_for_model(history, int(os.getenv("GEMINI_INTAKE_HISTORY_CHARS", "80000")))
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

    candidate = _normalize({**first, **labelled, **fast_structured})

    if document_type in {"affidavit", "evidence_pw_affidavit"}:
        candidate = _normalize(_rescue_affidavit_fields(message, candidate, current))

    missing = [key for key in required if not candidate.get(key) and not current.get(key)]

    audit: dict[str, Any] = {}
    if missing and len(combined_source) < 3500 and os.getenv("GEMINI_INTAKE_AUDIT_ALWAYS", "0").strip().lower() in {"1", "true", "yes", "on"}:
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
    for key, value in audit.items():
        if value in (None, "", [], {}):
            continue
        if key in {
            "plaintiffs", "defendants", "parties", "facts", "reliefs", "interim_reliefs",
            "demands", "defence_points", "documents", "custom_add_points", "custom_remove_points"
        }:
            merged[key] = _dedupe(list(merged.get(key, [])) + (value if isinstance(value, list) else [value]))
        else:
            merged[key] = value

    merged = _normalize(_narrative_rescue(message, merged))
    if document_type in {"affidavit", "evidence_pw_affidavit"}:
        merged = _normalize(_rescue_affidavit_fields("\n".join(history), merged, current))
    return merged


def reconcile_case_state(
    current_facts: dict[str, Any] | None,
    document_type: str,
    user_history: list[str],
) -> dict[str, Any]:
    history = [str(x) for x in (user_history or []) if str(x).strip()]
    if not history:
        return _normalize(current_facts or {})
    return extract_case_facts(
        history[-1],
        current_facts=current_facts or {},
        document_type=document_type,
        user_history=history,
    )
