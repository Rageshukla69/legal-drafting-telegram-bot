"""Vakil Drafting AI — Enterprise Hindi Legal Drafting & Judicial Pleading Suite.

Supports multi-style judicial drafting (Standard Judicial Format, Senior Counsel Edition,
Vakil AI Pro Hybrid, As-It-Is / Handwritten-to-Legal-Size Conversion Mode, and Single-Page
Recreation & Correction Mode), Scope-Aware Smart Sansodhan Intelligence (Targeted vs. Global
1-tap scope toggle), 1-Tap Undo Rollback, Pre-Filing Smart Draft Audit, Section & Paragraph Map,
automatic blueprint matching, multi-document case chaining (Vaad Patra, Jawab Dava, Interim
Application, Affidavit, Legal Notice), universal GPT-6.0-Luna Affidavit engine with canonical
structure, strict Order VI Rule 2 CPC section-free pleadings/Jawab Dava, bilingual numerical
formatting, and voice/text/OCR/DOCX intake with multi-part split-message reassembly.
"""
from __future__ import annotations
import asyncio, json, logging, os, re, tempfile
from io import BytesIO
from pathlib import Path

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
except ImportError:  # pragma: no cover - minimal test environments
    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data
    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard
    class Update:
        ALL_TYPES = None
    Application = CallbackQueryHandler = CommandHandler = ContextTypes = MessageHandler = filters = None

from app.case_store import CaseStore
from app.access_control import AccessController
from app.drafting_engine.conversation_state import (
    CaseState, DISPLAY_NAMES, STYLE_NAMES, missing_fields, next_questions
)
from app.drafting_engine.multi_draft_orchestrator import MultiDraftOrchestrator
from app.drafting_engine.renderers.legal_document_renderer import render_both, extract_pdf_page_slice
from app.drafting_engine.azure_speech import transcribe_voice
from app.drafting_engine.gemini_client import GeminiError
from app.drafting_engine.draft_editor import (
    DraftEditor,
    audit_draft_consistency,
    convert_edit_scope,
    count_occurrences_across_draft,
    format_draft_section_map,
)
from app.drafting_engine.document_intelligence import (
    analyze_document_bytes,
    extract_pdf_pages_list,
    DocumentIntelligenceError,
)
from app.drafting_engine.krutidev_converter import decode_krutidev_hybrid

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("legal-bot")

store = CaseStore()
access = AccessController(store, allow_unset=True)
orchestrator = MultiDraftOrchestrator()
editor = DraftEditor()

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
TELEGRAM_SAFE_MAX_CHARS = 3900
_DEVANAGARI_DIGITS_TRANS = str.maketrans("०१२३४५६७८९", "0123456789")

# Per-user concurrency locks and split-message buffers so long pasted texts split by Telegram
# into 2-4 parts at the same millisecond are seamlessly reassembled and processed once.
_user_locks: dict[int, asyncio.Lock] = {}
_pending_text_buffers: dict[int, list[str]] = {}


def _get_user_lock(uid: int) -> asyncio.Lock:
    lock = _user_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[uid] = lock
    return lock


TYPE_ORDER = [
    "dava_plaint", "written_statement", "application",
    "evidence_pw_affidavit", "affidavit", "legal_notice", "other_civil"
]
TYPE_ICONS = {
    "dava_plaint": "📜", "written_statement": "🛡️", "application": "⚖️",
    "evidence_pw_affidavit": "🎙️", "affidavit": "📋", "legal_notice": "📬", "other_civil": "📑"
}

BLUEPRINT_LABELS = {
    "dava_plaint": "Civil Suit Plaint Blueprint (दीवानी वाद-पत्र प्रारूप)",
    "written_statement": "Written Statement Defense Blueprint (प्रतिवाद-पत्र / जवाब दावा प्रारूप)",
    "application": "Procedural Court Application Blueprint (न्यायालय प्रार्थना-पत्र प्रारूप)",
    "evidence_pw_affidavit": "Examination-in-Chief Blueprint (साक्ष्य बयान चीफ प्रारूप)",
    "affidavit": "Universal Judicial & Executive Affidavit Blueprint (शपथ-पत्र मानक प्रारूप)",
    "legal_notice": "Statutory Legal Notice Blueprint (विधिक नोटिस प्रारूप)",
    "other_civil": "Civil Judicial Proceeding Blueprint (सिविल न्यायिक प्रारूप)",
}


def case_id_for(user_id: int) -> str:
    return f"tg-{user_id}-current"


def get_state(uid: int):
    return store.get(case_id_for(uid))


def _truncate_for_telegram(text: str, limit: int = TELEGRAM_SAFE_MAX_CHARS) -> str:
    s = str(text or "")
    if len(s) <= limit:
        return s
    cut = s.rfind("\n", 0, limit - 120)
    if cut < limit // 2:
        cut = limit - 120
    return s[:cut].rstrip() + "\n\n… _(संपूर्ण पाठ केस डोजियर में सुरक्षित है)_"


async def _safe_reply_text(message, text: str, **kwargs):
    """Send a reply with Markdown and automatic 3900-char clamping, falling back to plain text if dynamic user/draft text has unescaped symbols."""
    safe_text = _truncate_for_telegram(text)
    try:
        return await message.reply_text(safe_text, **kwargs)
    except Exception:
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("parse_mode", None)
        plain = _truncate_for_telegram(safe_text.replace("*", "").replace("`", "").replace("_", ""), 3700)
        return await message.reply_text(plain, **fallback_kwargs)


async def _safe_edit_text(query, text: str, **kwargs):
    """Edit a callback message with Markdown and automatic 3900-char clamping, falling back to plain text if unescaped symbols exist."""
    safe_text = _truncate_for_telegram(text)
    try:
        return await query.edit_message_text(safe_text, **kwargs)
    except Exception:
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("parse_mode", None)
        plain = _truncate_for_telegram(safe_text.replace("*", "").replace("`", "").replace("_", ""), 3700)
        return await query.edit_message_text(plain, **fallback_kwargs)


def format_public_blueprint_name(raw_name: str, document_type: str = "dava_plaint", style_mode: str = "template_mode") -> str:
    """Convert internal corpus filenames into confidential, professional judicial blueprint identifiers."""
    if style_mode == "single_page_mode":
        return "VD-PAGE • Single-Page Exact Legal Recreation (एकल पन्ना पुनर्निर्माण प्रारूप)"
    if style_mode == "as_is_mode":
        return "VD-ASIS • Verbatim Legal Size Format (हूबहू / हस्तलिखित लीगल पेपर प्रारूप)"
    prefix = "VD-STD" if style_mode == "template_mode" else ("VD-SR" if style_mode == "dad_style" else "VD-PRO")
    base_label = BLUEPRINT_LABELS.get(document_type, BLUEPRINT_LABELS["other_civil"])
    if document_type == "affidavit":
        return f"{prefix} • {base_label}"
    raw = str(raw_name or "").strip()
    if not raw or "@" in raw or "shukla" in raw.lower() or "शुक्ला" in raw or "+" in raw:
        return f"{prefix} • {base_label}"
    cleaned = re.sub(r"\.(docx|doc|pdf|txt|rtf)$", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"_unicode$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\d+[_\-\s]*", "", cleaned)
    cleaned = cleaned.replace("_", " ").strip()
    if not cleaned or len(cleaned) > 55:
        return f"{prefix} • {base_label}"
    return f"{prefix} • {cleaned}"


def quota_progress_bar(used: int, limit: int) -> str:
    if limit <= 0:
        return "██████████ Unlimited"
    pct = min(100, int((used / max(1, limit)) * 100))
    filled = min(10, max(0, round(pct / 10)))
    empty = 10 - filled
    return f"{'█' * filled}{'░' * empty} ({used}/{limit})"


def style_selection_menu():
    """Executive menu for selecting the Legal Drafting Engine Style."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏛️ मानक न्यायिक प्रारूप (Standard Judicial)", callback_data="style:template_mode")],
        [InlineKeyboardButton("⚖️ वरिष्ठ अधिवक्ता शैली (Senior Counsel)", callback_data="style:dad_style")],
        [InlineKeyboardButton("✨ Vakil AI Pro — हाइब्रिड संस्करण", callback_data="style:hybrid_mode")],
        [InlineKeyboardButton("📄 हूबहू / हस्तलिखित → लीगल पेपर (As-It-Is)", callback_data="style:as_is_mode")],
        [InlineKeyboardButton("📃 एकल पन्ना सुधार / री-क्रिएट (Single Page)", callback_data="style:single_page_mode")],
    ])


def switch_style_menu(current_style: str = "template_mode"):
    rows = []
    for mode_key, label in STYLE_NAMES.items():
        prefix = "✅ " if mode_key == current_style else "🔹 "
        rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"regen_style:{mode_key}")])
    rows.append([InlineKeyboardButton("↩️ वापस जाएं (Back to Case)", callback_data="review")])
    return InlineKeyboardMarkup(rows)


def dashboard():
    rows = []
    for i in range(0, len(TYPE_ORDER), 2):
        row = []
        for t in TYPE_ORDER[i:i + 2]:
            row.append(InlineKeyboardButton(f"{TYPE_ICONS[t]} {DISPLAY_NAMES[t]}", callback_data=f"type:{t}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("📄 हूबहू लीगल पेपर (As-It-Is)", callback_data="style:as_is_mode"),
        InlineKeyboardButton("📃 एकल पन्ना सुधार (Single Page)", callback_data="style:single_page_mode"),
    ])
    rows.append([InlineKeyboardButton("🔄 ड्राफ्टिंग शैली बदलें (Change Style)", callback_data="choose_style")])
    return InlineKeyboardMarkup(rows)


def case_actions(state):
    rows = []
    if state.draft:
        rows.append([
            InlineKeyboardButton(f"📥 DOCX व PDF डाउनलोड करें (v{state.draft_version})", callback_data="download_current")
        ])
        rows.append([
            InlineKeyboardButton("✏️ स्मार्ट संशोधन (Smart Edit)", callback_data="edit_mode"),
            InlineKeyboardButton("📑 सभी पैरा व हेडिंग देखें", callback_data="view_sections"),
        ])
        rows.append([
            InlineKeyboardButton("➕ बिंदु जोड़ें (Add Point)", callback_data="add_point_mode"),
            InlineKeyboardButton("🗑️ बिंदु हटाएं (Remove Point)", callback_data="remove_point_mode"),
        ])
        audit_and_page_row = [
            InlineKeyboardButton("🔍 त्रुटि जांच (Audit)", callback_data="audit_draft"),
            InlineKeyboardButton("📃 एकल पन्ना निकालें (Single Page)", callback_data="single_page_studio"),
        ]
        rows.append(audit_and_page_row)
        if len(getattr(state, "draft_versions", []) or []) > 1:
            rows.append([
                InlineKeyboardButton(f"⏪ पूर्ववत करें (Undo to v{state.draft_version - 1})", callback_data="undo_draft"),
                InlineKeyboardButton("🔄 शैली बदलें (Switch Style)", callback_data="switch_style"),
            ])
        else:
            rows.append([
                InlineKeyboardButton("🔄 शैली बदलें (Switch Style)", callback_data="switch_style"),
            ])
        rows.append([
            InlineKeyboardButton("📑 अगला दस्तावेज़ बनाएं (Jawab Dava / शपथपत्र)", callback_data="add_document")
        ])
        rows.append([
            InlineKeyboardButton("🔄 नया ड्राफ्ट जनरेट करें (Regenerate)", callback_data="generate"),
            InlineKeyboardButton("📋 केस विवरण (Review)", callback_data="review"),
        ])
    else:
        if state.style_mode == "single_page_mode" or getattr(state, "single_page_mode", False):
            pg_num = max(1, int(getattr(state, "target_page_number", 0) or state.facts.get("target_page_number") or 1))
            gen_label = f"⚡ केवल पृष्ठ {pg_num} तैयार करें (Recreate Page {pg_num})"
        elif state.style_mode == "as_is_mode":
            gen_label = "⚡ हूबहू लीगल ड्राफ्ट तैयार करें (Generate Legal)"
        else:
            gen_label = "⚡ ड्राफ्ट तैयार करें (Generate)"
        rows.append([
            InlineKeyboardButton(gen_label, callback_data="generate"),
            InlineKeyboardButton("📋 केस विवरण (Review)", callback_data="review"),
        ])
        if state.style_mode not in {"as_is_mode", "single_page_mode"} and (state.facts.get("raw_source_text") or state.facts.get("facts")):
            rows.append([
                InlineKeyboardButton("📄 हूबहू लीगल पेपर में बनाएं (As-It-Is Legal)", callback_data="generate_as_is"),
                InlineKeyboardButton("📃 केवल 1 पन्ना बनाएं (Single Page)", callback_data="single_page_studio"),
            ])
        rows.append([
            InlineKeyboardButton("🔄 ड्राफ्टिंग शैली बदलें (Switch Style)", callback_data="choose_style")
        ])
        if state.documents:
            rows.append([
                InlineKeyboardButton("📑 अगला दस्तावेज़ जोड़ें (Add Document)", callback_data="add_document")
            ])

    if len(state.documents) > 1:
        rows.append([InlineKeyboardButton(f"🗂️ केस के सभी दस्तावेज़ ({len(state.documents)})", callback_data="case_documents")])
    rows.append([InlineKeyboardButton("🗑️ सेशन समाप्त करें (Close Session)", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def single_page_selector_menu(state: CaseState | None = None):
    """Build an inline keyboard allowing the user to pick which page (Page 1..6) to recreate or extract."""
    raw_pages = (state.facts.get("raw_source_pages") if state else None) or []
    max_pg = max(4, min(8, len(raw_pages))) if raw_pages else 5
    rows = []
    if state and state.draft:
        btn_row_1 = [
            InlineKeyboardButton(f"📄 PDF पृष्ठ {p} निकालें", callback_data=f"export_pdf_page:{p}")
            for p in range(1, min(4, max_pg + 1))
        ]
        rows.append(btn_row_1)
        if max_pg >= 4:
            btn_row_2 = [
                InlineKeyboardButton(f"📄 PDF पृष्ठ {p} निकालें", callback_data=f"export_pdf_page:{p}")
                for p in range(4, min(7, max_pg + 1))
            ]
            rows.append(btn_row_2)
    recreate_row_1 = [
        InlineKeyboardButton(f"📃 पृष्ठ {p} री-क्रिएट करें", callback_data=f"select_recreate_page:{p}")
        for p in range(1, 4)
    ]
    recreate_row_2 = [
        InlineKeyboardButton(f"📃 पृष्ठ {p} री-क्रिएट करें", callback_data=f"select_recreate_page:{p}")
        for p in range(4, 7)
    ]
    rows.append(recreate_row_1)
    rows.append(recreate_row_2)
    rows.append([InlineKeyboardButton("↩️ वापस जाएं (Back)", callback_data="review")])
    return InlineKeyboardMarkup(rows)


def document_menu(state=None):
    rows = []
    for i in range(0, len(TYPE_ORDER), 2):
        rows.append([
            InlineKeyboardButton(f"{TYPE_ICONS[t]} {DISPLAY_NAMES[t]}", callback_data=f"addtype:{t}")
            for t in TYPE_ORDER[i:i + 2]
        ])
    rows.append([InlineKeyboardButton("🔄 अगले दस्तावेज़ हेतु शैली बदलें", callback_data="choose_style")])
    rows.append([InlineKeyboardButton("↩️ वर्तमान दस्तावेज़ पर वापस जाएं", callback_data="review")])
    return InlineKeyboardMarkup(rows)


def existing_documents_menu(state):
    rows = []
    for item in state.documents[-20:][::-1]:
        did = str(item.get("document_id", ""))
        typ = str(item.get("document_type", ""))
        if not did or typ not in DISPLAY_NAMES:
            continue
        marker = "✅ " if did == state.active_document_id else "📄 "
        rows.append([InlineKeyboardButton(f"{marker}{DISPLAY_NAMES[typ]}", callback_data=f"doc:{did}")])
    rows.append([InlineKeyboardButton("➕ नया दस्तावेज़ जोड़ें", callback_data="add_document")])
    return InlineKeyboardMarkup(rows)


# Ordered so specific document types (Evidence Affidavit, Affidavit, Application/Objection, Legal Notice, Written Statement)
# are checked cleanly without false positives when 'जवाबदावा' is mentioned inside a paragraph of an Objection Petition.
_TYPE_KEYWORDS = {
    "evidence_pw_affidavit": (
        "evidence affidavit", "pw affidavit", "साक्ष्य शपथपत्र", "साक्ष्य का शपथपत्र",
        "साक्ष्य हेतु शपथ", "बयान चीफ", "पी डब्लू", "डी डब्लू", "pw-1", "dw-1", "pw1",
    ),
    "affidavit": (
        "affidavit", "शपथपत्र", "शपथ पत्र", "शपथ-पत्र", "हलफनामा", "हल्फ़नामा", "हलफ़नामा",
        "शपथग्रहीता", "शपथग्रहीत्री", "शपथकर्ता", "शपथकर्त्री", "शपथपूर्वक", "तस्दीक",
        "shapath patra", "shapathpatra", "halafnama",
    ),
    "application": (
        "application", "प्रार्थना पत्र", "प्रार्थना-पत्र", "प्रार्थनापत्र", "आपत्ति पत्र",
        "आपत्ति-पत्र", "आपत्तिपत्र", "आपत्तियाँ सादर", "आपत्तियां सादर", "पुनर्स्थापन प्रार्थना पत्र",
        "आदेश 9 नियम 13", "आदेश ९ नियम १३", "आवेदन", "कमीशन", "6ग", "संशोधन",
        "39 नियम 1", "7 नियम 11", "स्टे प्रार्थना पत्र", "objection petition",
    ),
    "written_statement": (
        "written statement", "जवाब दावा", "जवाब-दावा", "प्रतिवादपत्र", "प्रतिवाद पत्र",
        "जवाबदावा", "लिखित कथन", "jawab dava", "jawabdava",
    ),
    "legal_notice": (
        "legal notice", "कानूनी नोटिस", "विधिक नोटिस", "रजिस्टर्ड नोटिस", "138 ni", "धारा 138",
    ),
    "other_civil": (
        "other civil", "अन्य सिविल", "सिविल ड्राफ्ट", "कैवियट", "केवियट", "caveat", "तलाशपता", "इकरारनामा",
    ),
    "dava_plaint": (
        "dava", "दावा", "वाद पत्र", "वाद-पत्र", "वादपत्र", "plaint", "plaintiff suit",
        "स्थायी निषेधाज्ञा", "बंटवारा", "विशिष्ट अनुपालन",
    ),
}


def infer_document_type(text: str, default: str = "dava_plaint") -> str:
    """Infer document type from natural-language Hindi/English dictation or OCR text,
    checking the document's heading/title (first 400 chars) first so incidental mentions
    of 'जवाबदावा' or 'वादपत्र' inside body paragraphs do not misclassify an Application/Objection."""
    raw = str(text or "").strip()
    if not raw:
        return default
    head_low = re.sub(r"\s+", " ", raw[:400].casefold())
    full_low = re.sub(r"\s+", " ", raw.casefold())

    # 1. Check heading/title region first
    if any(m in head_low for m in ("आपत्ति पत्र", "आपत्ति-पत्र", "आपत्तिपत्र", "प्रार्थना पत्र", "प्रार्थना-पत्र", "आदेश 9 नियम 13", "आदेश ९ नियम १३", "objection petition")):
        return "application"
    for typ, markers in _TYPE_KEYWORDS.items():
        if any(m in head_low for m in markers):
            return typ

    # 2. Check full text if heading didn't match
    if any(m in full_low for m in ("written statement", "जवाब दावा", "जवाब-दावा", "प्रतिवादपत्र", "प्रतिवाद पत्र", "jawab dava", "jawabdava")):
        return "written_statement"
    for typ, markers in _TYPE_KEYWORDS.items():
        if any(m in full_low for m in markers):
            return typ
    return default


def detect_single_page_intent(text: str) -> tuple[bool, int, str]:
    """Detect when a user explicitly asks to recreate or fix a specific single page (e.g., 'पेज 3 में नाम सही करके सिर्फ पेज 3 बना दो').
    Returns (is_single_page_request, page_number, correction_instruction).
    """
    raw = str(text or "").strip()
    if not raw:
        return False, 0, ""
    norm = raw.translate(_DEVANAGARI_DIGITS_TRANS)
    low = re.sub(r"\s+", " ", norm.casefold())

    single_page_cues = (
        "सिर्फ पेज", "केवल पेज", "सिर्फ पृष्ठ", "केवल पृष्ठ", "उसी पेज", "वही पेज",
        "उसी पन्ने", "सिर्फ पन्ना", "केवल पन्ना", "एक पन्ना", "सिंगल पेज",
        "only page", "single page", "just page", "recreate page", "replace page",
    )
    m_pg = re.search(r"(?:पेज|पृष्ठ|पन्ना|page|pg\.?)\s*(?:नं०|नं\.|नंबर|संख्या|no\.?)?\s*(\d{1,2})\b", low)
    if any(c in low for c in single_page_cues):
        pg_no = int(m_pg.group(1)) if m_pg else 1
        return True, max(1, pg_no), raw

    # Also match "पेज 3 को दोबारा बना दो / री-क्रिएट कर दो"
    if m_pg and any(w in low for w in ("दोबारा बना", "फिर से बना", "री-क्रिएट", "recreate", "अलग से बना", "प्रिंट कर")):
        pg_no = int(m_pg.group(1))
        return True, max(1, pg_no), raw

    return False, 0, ""


def detect_as_is_intent(text: str) -> bool:
    """Detect when a user explicitly asks to reproduce an A4 or handwritten document as-it-is in Legal size format."""
    low = re.sub(r"\s+", " ", str(text or "").strip().casefold())
    if not low:
        return False
    as_is_markers = (
        "as it is", "as-it-is", "हूबहू", "ज्यों का त्यों", "सेम टू सेम", "same to same",
        "लीगल साइज में", "लीगल पेपर में", "legal size", "legal paper",
        "हस्तलिखित", "हाथ से लिखा", "handwritten", "handwriting", "retype", "verbatim",
    )
    return any(m in low for m in as_is_markers)


def detect_additional_document_type(text: str):
    """Detect an explicit request to create another document while a draft exists."""
    low = re.sub(r"\s+", " ", str(text or "").strip().casefold())
    if not low:
        return None
    if detect_single_page_intent(text)[0]:
        return None
    create_markers = (
        "बना", "बनाओ", "बना दो", "तैयार", "तैयार कर", "ड्राफ्ट", "create",
        "generate", "draft", "make", "prepare", "चाहिए"
    )
    if not any(x in low for x in create_markers):
        return None
    return infer_document_type(low, default="") or None


def detect_edit_intent(text: str) -> bool:
    """Detect when a user message/voice note is a correction/revision or point add/remove instruction."""
    low = re.sub(r"\s+", " ", str(text or "").strip().casefold())
    if not low:
        return False
    edit_markers = (
        "बदल", "बदलो", "बदलना", "कर दो", "करो", "हटा", "हटाओ", "हटाना",
        "जोड़", "जोड़ो", "जोड़ना", "संशोधन", "सुधार", "सुधारना", "संशोधित",
        "की जगह", "के स्थान पर", "पैराग्राफ", "धारा", "प्रार्थना में", "सत्यापन में",
        "शीर्षक में", "पक्षकार में", "दूसरे पेज", "अगले पेज", "नए पेज", "गलती",
        "ठीक कर", "सही कर", "काट दो", "लिख दो", "लगा दो", "शुद्ध", "पॉइंट", "बिंदु",
        "सिर्फ पैरा", "केवल पैरा", "हर जगह", "सभी जगह", "पूरे ड्राफ्ट", "हस्ताक्षर",
        "replace", "change", "edit", "delete", "remove", "insert", "add", "point",
        "correct", "update", "modify", "amend", "next page", "page 2", "only in", "everywhere",
    )
    return any(m in low for m in edit_markers)


def edit_actions(state: CaseState | None = None):
    """Build Change Preview action buttons, including a 1-tap Scope Toggle button (Targeted <-> Global)
    whenever the edited phrase appears in multiple places across the draft."""
    rows = [
        [
            InlineKeyboardButton("✅ संशोधन लागू करें (Apply)", callback_data="edit_apply"),
            InlineKeyboardButton("❌ निरस्त करें (Discard)", callback_data="edit_discard"),
        ]
    ]
    if state and state.draft and state.pending_edit:
        edit = state.pending_edit.get("edit") or {}
        op = str(edit.get("operation") or "")
        old_text = str(edit.get("old_text") or "").strip()
        if not old_text and edit.get("edits"):
            first_sub = (edit.get("edits") or [{}])[0]
            old_text = str(first_sub.get("old_text") or "").strip()
        if old_text and op in {"replace", "replace_all"}:
            matches = count_occurrences_across_draft(state.draft, old_text)
            if len(matches) > 1:
                if op == "replace":
                    rows.append([
                        InlineKeyboardButton(
                            f"🌐 पूरे ड्राफ्ट में सभी {len(matches)} स्थानों पर बदलें (Apply Everywhere)",
                            callback_data="toggle_scope:global",
                        )
                    ])
                elif op == "replace_all":
                    rows.append([
                        InlineKeyboardButton(
                            "🎯 केवल लक्षित पैरा/हेडिंग में बदलें (Only First Target)",
                            callback_data="toggle_scope:targeted",
                        )
                    ])
    return InlineKeyboardMarkup(rows)


def progress(state):
    missing = missing_fields(state.facts, state.document_type, style_mode=state.style_mode)
    total = len(missing_fields({}, state.document_type, style_mode="template_mode"))
    done = max(0, total - len(missing))
    pct = 100 if not missing else (int(done / total * 100) if total else 100)
    filled = "█" * max(1, pct // 10)
    empty = "░" * (10 - len(filled))
    return f"{filled}{empty} {pct}%"


def questions_text(state):
    style_label = STYLE_NAMES.get(state.style_mode, state.style_mode)
    if state.style_mode == "single_page_mode" or getattr(state, "single_page_mode", False):
        pg_no = max(1, int(getattr(state, "target_page_number", 0) or state.facts.get("target_page_number") or 1))
        if state.facts.get("raw_source_text") or state.facts.get("facts"):
            corr = getattr(state, "page_correction_instruction", "") or state.facts.get("page_correction_instruction") or "हूबहू पुनर्निर्माण"
            return (
                f"🎨 *ड्राफ्टिंग मोड:* {style_label}\n"
                f"📃 *लक्षित पृष्ठ संख्या:* `पृष्ठ {pg_no}`\n"
                f"✏️ *निर्देशित सुधार:* {corr}\n"
                f"{DIVIDER}\n"
                "✅ *आपका पन्ना प्राप्त हो चुका है!*\n"
                "• यदि आपको इस पन्ने में कोई नाम, वर्ष, तारीख या शब्द बदलना है तो उसे सीधे लिखकर भेजें (जैसे: `पैरा 9 में वर्ष 2024 के स्थान पर 2025 कर दो`),\n"
                f"• या सीधे नीचे *⚡ केवल पृष्ठ {pg_no} तैयार करें* बटन दबाएँ।"
            )
        return (
            f"🎨 *ड्राफ्टिंग मोड:* {style_label}\n"
            f"{DIVIDER}\n"
            "📥 *किसी पुराने केस का केवल एक पन्ना (Single Page) सुधार के साथ दोबारा बनाने हेतु:*\n"
            "1️⃣ उस पन्ने (जैसे Page 2 या Page 3) की **Photo 📸 / PDF 📄 / Text ✍️** भेजें,\n"
            "2️⃣ साथ में लिखें या बोलकर (Voice Note 🎙️) बताएं कि कौन-सा पेज नंबर है और उसमें क्या सुधार करना है _(जैसे: `यह पेज 3 है, इसमें पैरा 9 में राम कुमार की जगह श्याम कुमार और सन 2023 की जगह 2024 कर दो`)_।\n\n"
            "_Bot ठीक उसी पैरा नंबर (जैसे `9 (नौ) :`) और उसी पृष्ठ संख्या (`पृष्ठ ३`) के साथ केवल वही एकल पन्ना Legal Size DOCX व PDF में तैयार कर देगा!_"
        )

    if state.style_mode == "as_is_mode":
        if state.facts.get("raw_source_text") or state.facts.get("facts"):
            return (
                f"🎨 *ड्राफ्टिंग मोड:* {style_label}\n"
                f"{DIVIDER}\n"
                "✅ *आपका दस्तावेज़ प्राप्त हो चुका है!*\n"
                "• यदि कोई और पेज बाकी हो तो उसकी Photo/PDF भेजें,\n"
                "• या सीधे नीचे *⚡ हूबहू लीगल ड्राफ्ट तैयार करें (Generate Legal)* बटन दबाएँ।"
            )
        return (
            f"🎨 *ड्राफ्टिंग मोड:* {style_label}\n"
            f"{DIVIDER}\n"
            "📥 *कृपया अपने A4 कागज़ या हस्तलिखित (Handwritten) दस्तावेज़ को भेजें:*\n"
            "• अपने छपे हुए A4 ड्राफ्ट या हाथ से लिखे पन्नों की **Photo 📸 / PDF 📄 / Word (.docx)** भेजें,\n"
            "• या **Text ✍️ / Voice Note 🎙️** भेजें।\n\n"
            "_Bot आपके दस्तावेज़ की मूल भाषा व क्रम को सुरक्षित रखते हुए उसे उचित विधिक ढांचे एवं कोर्ट-मानक **Legal Size Paper (DOCX + PDF)** में तैयार कर देगा, जिसके बाद आप उसमें कोई भी संशोधन कर सकेंगे।_"
        )

    qs = next_questions(state.facts, state.document_type, style_mode=state.style_mode)
    if not qs:
        return (
            f"🎨 *ड्राफ्टिंग शैली:* {style_label}\n"
            f"{DIVIDER}\n"
            "✅ *सभी आवश्यक तथ्य प्राप्त हो चुके हैं।*\n"
            "ड्राफ्ट तैयार करने के लिए नीचे *⚡ ड्राफ्ट तैयार करें (Generate)* बटन दबाएँ।"
        )
    if state.document_type == "affidavit":
        return (
            f"🎨 *ड्राफ्टिंग शैली:* {style_label}\n"
            f"{DIVIDER}\n"
            "📥 *कृपया शपथ-पत्र (Affidavit) का विवरण भेजें*\n"
            "_(आप किसी भी प्रकार का शपथ-पत्र — न्यायालय, विश्वविद्यालय/CSJMU, छात्रवृत्ति/OTR, समाज कल्याण, तहसील या नोटरी शपथ-पत्र Voice Note 🎙️, Text ✍️, या Photo/PDF 📸 से भेज सकते हैं)_:\n\n"
            + "\n".join(f"🔹 *{label}*" for _, label in qs)
            + "\n🔹 *समक्ष प्राधिकारी / न्यायालय का नाम* _(जैसे: कुलसचिव महोदय / समाज कल्याण विभाग / सिविल जज)_"
            + "\n🔹 *अधिवक्ता / पहचानकर्ता का विवरण* _(यदि अंकित करना हो)_"
        )
    return (
        f"🎨 *ड्राफ्टिंग शैली:* {style_label}\n"
        f"{DIVIDER}\n"
        "📥 *कृपया केस से संबंधित निम्नलिखित विवरण भेजें*\n"
        "_(आप एक साथ Voice Note 🎙️, Text ✍️, या दस्तावेज़ की Photo/PDF 📸 भेज सकते हैं)_:\n\n"
        + "\n".join(f"🔹 *{label}*" for _, label in qs)
        + "\n🔹 *अधिवक्ता का नाम व न्यायालय परिसर* _(यदि ड्राफ्ट में अंकित करना हो)_"
        + "\n🔹 *कोई विशेष विधिक बिंदु* _(जो आप जोड़ना चाहते हों)_"
    )


def summary_text(state):
    """Build a concise, Telegram-safe Case Dossier preview card (always < 1,500 chars)
    even when the underlying document has 17, 50, or 100+ long paragraphs."""
    facts = state.facts
    style_label = STYLE_NAMES.get(state.style_mode, state.style_mode)

    def val(k, max_items: int = 4, max_item_chars: int = 140) -> str:
        v = facts.get(k, "—")
        if isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
            if not items:
                return "—"
            shown = []
            for x in items[:max_items]:
                short_x = (x[:max_item_chars].rstrip() + "…") if len(x) > max_item_chars else x
                shown.append(f"  • {short_x}")
            if len(items) > max_items:
                rem = len(items) - max_items
                shown.append(f"  • _… (+ {rem} अन्य पैराग्राफ सुरक्षित रूप से दर्ज हैं — कुल {len(items)} पैराग्राफ)_")
            return "\n".join(shown)
        s = str(v or "—").strip()
        return (s[:300].rstrip() + "…") if len(s) > 300 else s

    extra = ""
    if state.selected_template_name or state.document_type == "affidavit" or state.style_mode in {"as_is_mode", "single_page_mode"}:
        bp_label = format_public_blueprint_name(state.selected_template_name, state.document_type, state.style_mode)
        extra += f"📘 *न्यायिक प्रारूप:* `{bp_label}`\n"
    if state.style_mode == "single_page_mode" or getattr(state, "single_page_mode", False):
        pg_num = max(1, int(getattr(state, "target_page_number", 0) or facts.get("target_page_number") or 1))
        extra += f"📃 *लक्षित पन्ना (Target Page):* `पृष्ठ {pg_num}`\n"
    if facts.get("advocate_name") or facts.get("court_complex"):
        adv_display = facts.get("advocate_name") or "अधिवक्ता"
        complex_display = f" ({facts.get('court_complex')})" if facts.get("court_complex") else ""
        extra += f"🎓 *अधिवक्ता / चैम्बर:* {adv_display}{complex_display}\n"
    if state.draft:
        extra += f"✅ *तैयार ड्राफ्ट संस्करण:* `v{state.draft_version}` _(नीचे '📥 DOCX व PDF डाउनलोड करें' दबाएं)_\n"

    if state.document_type in {"affidavit", "evidence_pw_affidavit"}:
        forum_str = facts.get("court_name") or "संबंधित सक्षम प्राधिकारी / न्यायालय"
        return (
            f"🏛️ *VAKIL DRAFTING AI — AFFIDAVIT DOSSIER*\n"
            f"{DIVIDER}\n"
            f"📄 *दस्तावेज़:* {DISPLAY_NAMES[state.document_type]}\n"
            f"🎨 *शैली:* {style_label}\n"
            f"{extra}"
            f"📊 *पूर्णता (Readiness):* `{progress(state)}`\n"
            f"{DIVIDER}\n"
            f"🏛️ *समक्ष / न्यायालय:* {forum_str}\n"
            f"👤 *शपथग्रहीता / शपथकर्ता:* {val('deponent', max_items=2)}\n"
            f"🎯 *उद्देश्य / विषय:* {val('purpose', max_items=2)}\n"
            f"📋 *शपथपूर्वक कथन (Statements):*\n{val('facts', max_items=4, max_item_chars=135)}\n"
        )

    parties_preview = val("parties", max_items=4, max_item_chars=110) if facts.get("parties") else val("plaintiffs", max_items=4, max_item_chars=110)
    relief_preview = val("reliefs", max_items=2, max_item_chars=160) if facts.get("reliefs") else val("demands", max_items=2, max_item_chars=160)
    return (
        f"🏛️ *VAKIL DRAFTING AI — CASE DOSSIER*\n"
        f"{DIVIDER}\n"
        f"📄 *दस्तावेज़:* {DISPLAY_NAMES[state.document_type]}\n"
        f"🎨 *शैली:* {style_label}\n"
        f"{extra}"
        f"📊 *पूर्णता (Readiness):* `{progress(state)}`\n"
        f"{DIVIDER}\n"
        f"🏛️ *न्यायालय:* {val('court_name', max_items=1, max_item_chars=140)}\n"
        f"👥 *पक्षकार:*\n{parties_preview}\n"
        f"📋 *मुख्य तथ्य / पैराग्राफ:*\n{val('facts', max_items=4, max_item_chars=135)}\n"
        f"⚖️ *मांगी गई राहत / प्रार्थना:*\n{relief_preview}\n"
    )


STYLE_PROMPT_TEXT = (
    "🏛️ *VAKIL DRAFTING AI — JUDICIAL SUITE*\n"
    f"{DIVIDER}\n"
    "*Enterprise Hindi Legal Drafting & Pleading Engine*\n\n"
    "नया केस या विधिक दस्तावेज़ तैयार करने के लिए अपनी पसंदीदा **Drafting Engine Style** चुनें:\n\n"
    "🏛️ *1. मानक न्यायिक प्रारूप (Standard Judicial)*\n"
    "दीवानी, राजस्व एवं फौजदारी न्यायालयों के प्रमाणित न्यायिक प्रारूपों (Verified Judicial Blueprints) के आधार पर सटीक, सुव्यवस्थित ड्राफ्ट तैयार करता है।\n\n"
    "⚖️ *2. वरिष्ठ अधिवक्ता शैली (Senior Counsel Edition)*\n"
    "वरिष्ठ अधिवक्ताओं की पारंपरिक एवं प्रभावशाली अदालती भाषा, मजबूत विधिक शब्दावली तथा विस्तृत पैराग्राफ संरचना में ड्राफ्ट तैयार करता है।\n\n"
    "✨ *3. Vakil AI Pro — हाइब्रिड संस्करण (Recommended)*\n"
    "प्रमाणित न्यायिक ढांचे और वरिष्ठ अधिवक्ता की प्रभावशाली कानूनी भाषा का सर्वोत्तम संयोजन।\n\n"
    "📄 *4. हूबहू / हस्तलिखित → लीगल पेपर (As-It-Is / Handwritten)*\n"
    "आपके पास पहले से मौजूद A4 कागज़ या हाथ से लिखे (Handwritten) दावा, जवाब दावा, शपथ-पत्र, कानूनी नोटिस आदि को बिना किसी मनमाने बदलाव के **ज्यों का त्यों (As-It-Is) उचित विधिक ढांचे एवं कोर्ट-मानक Legal Size Paper (DOCX + PDF)** में तैयार करता है।\n\n"
    "📃 *5. एकल पन्ना सुधार / री-क्रिएट (Single-Page Recreation — /page)*\n"
    "यदि किसी पुराने केस के किसी एक पन्ने (जैसे Page 3) में नाम, तारीख या वर्ष गलत छप गया हो, तो ठीक उसी पैरा नंबर और पृष्ठ संख्या के साथ केवल **वही एक पन्ना** सुधार कर तैयार करता है।\n"
    f"{DIVIDER}\n"
    "👇 *नीचे दिए गए बटन से अपनी ड्राफ्टिंग शैली चुनें:*"
)


async def deny(update):
    uid = update.effective_user.id if update.effective_user else "—"
    if update.message:
        await update.message.reply_text(
            "🏛️ *VAKIL DRAFTING AI — ACCESS RESTRICTED*\n"
            f"{DIVIDER}\n"
            "🔒 यह एक प्राइवेट प्रोफेशनल लीगल ड्राफ्टिंग सुइट है। आप अभी इस सेवा के लिए अधिकृत (authorized) नहीं हैं।\n\n"
            "🎁 *Trial Pass (5 Documents)* या *💎 Vakil AI Pro Membership* सक्रिय कराने के लिए एडमिन को अपनी Client ID भेजें:\n"
            f"🆔 *आपकी Client ID:* `{uid}`",
            parse_mode="Markdown",
        )


async def require_access(update) -> bool:
    user = update.effective_user
    if not user or not access.is_authorized(user.id):
        await deny(update)
        return False
    return True


async def myid(update, context):
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    status = access.get_plan_status(uid)
    await update.message.reply_text(
        "🏛️ *VAKIL DRAFTING AI — CLIENT IDENTITY*\n"
        f"{DIVIDER}\n"
        f"🆔 *Telegram Client ID:* `{uid}`\n"
        f"🏷️ *Membership Tier:* {status['plan_label']}\n"
        f"{DIVIDER}\n"
        "_सदस्यता सक्रिय या अपग्रेड कराने हेतु यह ID एडमिन को भेजें।_",
        parse_mode="Markdown",
    )


async def corpus_info(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return

    idx_path = Path(__file__).resolve().parent.parent / "corpus_index.json"
    total_docs = 0
    if idx_path.exists():
        try:
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            total_docs = len(data.get("documents", []))
        except Exception:
            pass

    msg = (
        "🏛️ *VAKIL DRAFTING AI — SYSTEM ARCHITECTURE*\n"
        f"{DIVIDER}\n"
        "⚡ *Drafting Core:* `Vakil Neural Legal Engine v5.0 (GPT-6.0-Luna)`\n"
        "🎯 *Smart Sansodhan:* `Scope-Aware (Targeted Para/Heading vs Global)`\n"
        "📃 *Single-Page Studio:* `Exact Page Recreation & PDF Page Slice (/page)`\n"
        "🛡️ *Pleading Guard:* `Order VI Rule 2 CPC (Pure Factual Pleadings)`\n"
        "🔢 *Bilingual Formatter:* `Active (अंक + हिंदी शब्द)`\n"
        f"📚 *Indexed Judicial Precedents:* `{max(total_docs, 800)}+ Verified Formats`\n"
        f"{DIVIDER}\n"
        "🎨 *उपलब्ध ड्राफ्टिंग इंजन मोड्स (/newcase, /style, /asis या /page):*\n"
        "• *🏛️ मानक न्यायिक प्रारूप:* प्रमाणित सिविल एवं राजस्व न्यायालय ब्लूप्रिंट।\n"
        "• *⚖️ वरिष्ठ अधिवक्ता शैली:* पारंपरिक एवं प्रभावशाली सीनियर काउंसिल कोर्ट भाषा।\n"
        "• *✨ Vakil AI Pro हाइब्रिड:* न्यायिक ढांचे और वरिष्ठ अधिवक्ता शब्दावली का संयुक्त संस्करण।\n"
        "• *📄 हूबहू / हस्तलिखित → लीगल पेपर (/asis):* A4 या हाथ से लिखे दस्तावेज़ को ज्यों का त्यों Legal Size DOCX व PDF में बदलें।\n"
        "• *📃 एकल पन्ना सुधार / री-क्रिएट (/page):* किसी केस का केवल एक विशेष पन्ना (जैसे Page 3) सुधार सहित ठीक उसी पैरा व पेज नंबर पर तैयार करें।"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def help_command(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    msg = (
        "🏛️ *VAKIL DRAFTING AI — EXECUTIVE USER GUIDE*\n"
        f"{DIVIDER}\n"
        "📌 *प्रमुख कमांड्स (Quick Commands):*\n"
        "• `/newcase` — नया केस या विधिक ड्राफ्ट शुरू करें\n"
        "• `/asis` — A4 कागज़ या हस्तलिखित (Handwritten) ड्राफ्ट को हूबहू Legal Size पेपर में बनाएं\n"
        "• `/page` — पुराने केस के किसी एक पन्ने (जैसे Page 3) में सुधार कर केवल वही पन्ना री-क्रिएट करें\n"
        "• `/edit` — स्मार्ट संशोधन (किसी खास पैरा/हेडिंग में या पूरे ड्राफ्ट में बदलाव करें)\n"
        "• `/sections` — ड्राफ्ट के सभी पैराग्राफ व हेडिंग की सूची देखें\n"
        "• `/audit` — कोर्ट में दाखिल करने से पहले खाली स्थान (`....`) व सत्यापन की जांच करें\n"
        "• `/undo` — पिछले ड्राफ्ट संस्करण (`v{N-1}`) पर तुरंत वापस जाएं\n"
        "• `/style` — ड्राफ्टिंग इंजन शैली बदलें\n"
        "• `/adddocument` — उसी केस के तथ्यों से जवाब दावा, शपथपत्र या प्रार्थना पत्र बनाएं\n"
        "• `/download` — वर्तमान तैयार/संशोधित ड्राफ्ट की DOCX व PDF फ़ाइलें प्राप्त करें\n"
        "• `/summary` — वर्तमान केस डोजियर एवं प्रगति देखें\n"
        "• `/myplan` — अपनी सदस्यता एवं शेष ड्राफ्ट कोटा देखें\n"
        "• `/cancel` — वर्तमान सेशन समाप्त करें\n"
        f"{DIVIDER}\n"
        "🎯 *स्मार्ट संशोधन (Scope-Aware Sansodhan) कैसे काम करता है?*\n"
        "• *केवल एक पैरा या हेडिंग में बदलाव:* लिखें `सिर्फ पैरा 3 में 2024 की जगह 2025 कर दो` या `शीर्षक में ... बदल दो` — Bot अन्य पैराग्राफों को छुए बिना केवल उसी स्थान पर बदलाव करेगा।\n"
        "• *पूरे ड्राफ्ट में सभी जगह बदलाव:* लिखें `पूरे ड्राफ्ट में राम कुमार के स्थान पर राम कुमार शर्मा कर दो` — या Preview में दिए गए **🌐 पूरे ड्राफ्ट में बदलें / 🎯 केवल इसी पैरा में बदलें** बटन से 1-टैप में दायरा बदलें!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def asis_command(update, context):
    """Quick command `/asis` to activate As-It-Is / Handwritten to Legal Paper mode."""
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    existing = get_state(uid)
    doc_type = existing.document_type if existing else "dava_plaint"
    state = CaseState(
        case_id_for(uid),
        document_type=doc_type,
        style_mode="as_is_mode",
        style_selected=True,
        type_selected=bool(existing and existing.type_selected),
    )
    store.save(uid, state)
    await update.message.reply_text(
        "🏛️ *VAKIL DRAFTING AI — AS-IT-IS & HANDWRITTEN LEGAL STUDIO*\n"
        f"{DIVIDER}\n"
        "✅ **📄 हूबहू / हस्तलिखित → लीगल पेपर मोड सक्रिय है!**\n\n"
        "• यदि आपके पास **A4 कागज़ पर छपा हुआ** कोई दावा, जवाब दावा, शपथ-पत्र, कानूनी नोटिस या प्रार्थना-पत्र है, या\n"
        "• यदि आपने **हाथ से कोई ड्राफ्ट (Handwritten Draft)** लिखा है जिसे आप उचित विधिक ढांचे (Proper Legal Structure) में ढालना चाहते हैं,\n\n"
        "तो नीचे से दस्तावेज़ का प्रकार चुनें या सीधे उसकी **Photo 📸, PDF 📄, Word (.docx), Text ✍️, या Voice Note 🎙️** भेजें। "
        "Bot उसे **Legal Size Paper (`8.5\" × 14\"`)** में तैयार कर देगा जिसके बाद आप उसमें कोई भी छोटा-बड़ा संशोधन (Edit) कर सकेंगे:",
        parse_mode="Markdown",
        reply_markup=dashboard(),
    )


async def page_command(update, context):
    """Command `/page [N] [instruction]` to recreate or extract a specific single page `N`."""
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    existing = get_state(uid)
    args = list(context.args or [])
    target_pg = 1
    inst_str = ""
    if args:
        first_arg = args[0].translate(_DEVANAGARI_DIGITS_TRANS)
        if first_arg.isdigit():
            target_pg = max(1, int(first_arg))
            inst_str = " ".join(args[1:]).strip()
        else:
            inst_str = " ".join(args).strip()

    if existing and (existing.draft or existing.facts.get("raw_source_pages")):
        existing.single_page_mode = True
        existing.target_page_number = target_pg
        if inst_str:
            existing.page_correction_instruction = inst_str
            existing.facts["page_correction_instruction"] = inst_str
        existing.facts["target_page_number"] = target_pg
        store.save(uid, existing)
        if args and first_arg.isdigit() and existing.facts.get("raw_source_pages"):
            existing.style_mode = "single_page_mode"
            store.save(uid, existing)
            await generate_for(update, uid, existing, edit=False)
            return
        await update.message.reply_text(
            "🏛️ *VAKIL DRAFTING AI — SINGLE-PAGE RECREATION & EXPORT STUDIO*\n"
            f"{DIVIDER}\n"
            "📃 **एकल पन्ना पुनर्निर्माण एवं एक्सपोर्ट स्टूडियो:**\n\n"
            "• यदि आप वर्तमान तैयार ड्राफ्ट में से **केवल एक विशेष पन्ना (PDF Page 1, 2, 3...)** निकालना चाहते हैं, तो नीचे **📄 PDF पृष्ठ निकालें** बटन दबाएं।\n"
            "• यदि आप किसी पुराने केस के किसी खास पन्ने (जैसे Page 3) में नाम/वर्ष सुधार कर केवल वही पन्ना री-क्रिएट करना चाहते हैं, तो नीचे से पृष्ठ संख्या चुनें या उस पन्ने की Photo/PDF भेजें:",
            parse_mode="Markdown",
            reply_markup=single_page_selector_menu(existing),
        )
        return

    state = CaseState(
        case_id_for(uid),
        document_type=existing.document_type if existing else "dava_plaint",
        style_mode="single_page_mode",
        style_selected=True,
        type_selected=True,
        single_page_mode=True,
        target_page_number=target_pg,
        page_correction_instruction=inst_str,
    )
    state.facts["target_page_number"] = target_pg
    if inst_str:
        state.facts["page_correction_instruction"] = inst_str
    store.save(uid, state)
    await update.message.reply_text(
        "🏛️ *VAKIL DRAFTING AI — SINGLE-PAGE RECREATION STUDIO*\n"
        f"{DIVIDER}\n"
        f"✅ **📃 एकल पन्ना सुधार / री-क्रिएट मोड (पृष्ठ {target_pg}) सक्रिय है!**\n\n"
        "कृपया उस पन्ने की **Photo 📸, पूरी PDF 📄, या Text ✍️** भेजें और बताएं कि उसमें क्या सुधार करना है।\n\n"
        "*उदाहरण:*\n"
        "• `यह पेज 3 है, इसके पैरा 9 में वर्ष 2022 के स्थान पर 2024 कर दो और केवल यही पेज बना दो।`\n"
        "• या पूरी Multi-Page PDF भेजकर लिखें: `पेज 2 में राम लाल की जगह श्याम लाल करके सिर्फ पेज 2 बना दो।`",
        parse_mode="Markdown",
        reply_markup=single_page_selector_menu(state),
    )


async def sections_command(update, context):
    """Show the complete section & numbered paragraph map of the active draft so the advocate can target any paragraph or heading."""
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state or not state.draft:
        await update.message.reply_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है। पहले ड्राफ्ट तैयार करें।")
        return
    map_text = format_draft_section_map(state.draft)
    await _safe_reply_text(
        update.message,
        f"🏛️ *VAKIL DRAFTING AI — DOCUMENT SECTION MAP (v{state.draft_version})*\n"
        f"{DIVIDER}\n"
        f"{map_text}\n"
        f"{DIVIDER}\n"
        "💡 _किसी विशेष पैरा या हेडिंग में संशोधन करने हेतु लिखें: `सिर्फ पैरा 3 में ... कर दो` या `शीर्षक में ... बदल दो`।_",
        parse_mode="Markdown",
        reply_markup=case_actions(state),
    )


async def audit_command(update, context):
    """Run Pre-Filing Smart Consistency & Blank-Placeholder Audit on the active draft."""
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state or not state.draft:
        await update.message.reply_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है। पहले ड्राफ्ट तैयार करें।")
        return
    findings = audit_draft_consistency(state.draft, state.document_type)
    await _safe_reply_text(
        update.message,
        f"🏛️ *VAKIL DRAFTING AI — PRE-FILING SMART AUDIT (v{state.draft_version})*\n"
        f"{DIVIDER}\n"
        + "\n\n".join(findings)
        + f"\n{DIVIDER}\n"
        "💡 _किसी भी रिक्त स्थान या त्रुटि को भरने के लिए सीधे लिखकर या Voice Note 🎙️ से निर्देश भेजें।_",
        parse_mode="Markdown",
        reply_markup=case_actions(state),
    )


async def undo_command(update, context):
    """Revert the active draft to the previous version (`v{N-1}`) with zero quota cost."""
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    state = get_state(uid)
    if not state or not state.draft:
        await update.message.reply_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है।")
        return
    if not state.undo_draft():
        await update.message.reply_text("ℹ️ यह ड्राफ्ट का प्रथम संस्करण (`v1`) है; इससे पूर्व कोई संस्करण उपलब्ध नहीं है।")
        return
    store.save(uid, state)
    await update.message.reply_text(
        f"⏪ *पूर्ववत सफल (Undo Complete):* आपका ड्राफ्ट वापस संस्करण `v{state.draft_version}` पर स्थापित कर दिया गया है।",
        parse_mode="Markdown",
    )
    await download_current_draft(update, uid, state, edit_message=False)


async def export_single_pdf_page(update, uid: int, state: CaseState, page_number: int):
    """Render the current draft to PDF and extract only page `page_number` as a standalone single-page PDF."""
    target = update.callback_query.message if update.callback_query else update.message
    if not state or not state.draft:
        await target.reply_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है।")
        return
    if update.callback_query:
        await _safe_edit_text(
            update.callback_query,
            f"⚙️ *{DISPLAY_NAMES[state.document_type]} (v{state.draft_version})* में से **केवल पृष्ठ {page_number}** निकाला जा रहा है…",
            parse_mode="Markdown",
        )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            label = DISPLAY_NAMES[state.document_type].split(" /")[0].replace(" ", "_")
            base_name = f"{label}_{uid}_v{state.draft_version}"
            _, full_pdf = await asyncio.to_thread(
                render_both,
                state.draft,
                tmp,
                base_name,
                os.getenv("LEGAL_PAPER", "legal"),
            )
            single_pdf_path = Path(tmp) / f"{base_name}_Page_{page_number}.pdf"
            await asyncio.to_thread(extract_pdf_page_slice, full_pdf, page_number, single_pdf_path)
            with open(single_pdf_path, "rb") as f:
                await target.reply_document(
                    f,
                    filename=single_pdf_path.name,
                    caption=f"📃 Single-Page Court PDF — {DISPLAY_NAMES[state.document_type]} (केवल पृष्ठ {page_number}, v{state.draft_version})",
                )
        await _safe_reply_text(
            target,
            f"✅ *पृष्ठ {page_number}* की एकल PDF फ़ाइल सफलतापूर्वक तैयार कर भेज दी गई है।",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
    except ValueError as ve:
        await _safe_reply_text(
            target,
            f"⚠️ {ve}",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
    except Exception as exc:
        log.exception("export_single_pdf_page failed")
        await target.reply_text(f"❌ एकल पृष्ठ निकालने में त्रुटि: {str(exc)[:300]}")


async def trial_command(update, context):
    """Owner/Sudo command #1: Authorize a user on a Trial basis (limited to 5 documents by default)."""
    if not update.message or not update.effective_user:
        return
    if not access.is_owner(update.effective_user.id):
        await deny(update)
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "🏛️ *VAKIL AI ADMIN — TRIAL PROVISIONING*\n"
            f"{DIVIDER}\n"
            "• डिफ़ॉल्ट 5-ड्राफ्ट ट्रायल: `/trial TELEGRAM_USER_ID`\n"
            "• कस्टम ट्रायल लिमिट: `/trial TELEGRAM_USER_ID 5`",
            parse_mode="Markdown",
        )
        return
    target = context.args[0]
    if access.is_owner(target):
        await update.message.reply_text("👑 Chamber Admin के पास पहले से Unlimited विशेषाधिकार हैं।")
        return
    limit = 5
    if len(context.args) >= 2 and context.args[1].isdigit() and int(context.args[1]) > 0:
        limit = int(context.args[1])
    access.authorize_trial(target, update.effective_user.id, limit=limit)
    await update.message.reply_text(
        "🏛️ *VAKIL AI ADMIN — TRIAL PASS ACTIVATED*\n"
        f"{DIVIDER}\n"
        f"👤 *Client ID:* `{target}`\n"
        f"🏷️ *Tier:* 🎁 Complimentary Trial Pass\n"
        f"📊 *Allocated Quota:* `{limit}` Legal Documents (`0/{limit}` used)\n"
        f"🟢 *Status:* Active & Ready",
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            chat_id=int(target),
            text=(
                "🏛️ *VAKIL DRAFTING AI — WELCOME PASS*\n"
                f"{DIVIDER}\n"
                f"🎁 *आपका Complimentary Trial Pass ({limit} Legal Documents) सक्रिय कर दिया गया है!*\n\n"
                f"• *उपलब्ध कोटा:* `{limit}` ड्राफ्ट (वाद-पत्र, जवाब दावा, प्रार्थना-पत्र, शपथपत्र आदि)\n"
                "• *ड्राफ्टिंग मोड्स:* Standard Judicial • Senior Counsel • Vakil AI Pro Hybrid • As-It-Is Legal • Single-Page Studio\n"
                "• *निःशुल्क संशोधन:* तैयार ड्राफ्ट में बिंदु जोड़ना या सुधार करना पूरी तरह निःशुल्क है।\n"
                f"{DIVIDER}\n"
                "👉 नया केस शुरू करने के लिए अभी `/start` या `/newcase` दबाएँ।"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def paid_command(update, context):
    """Owner/Sudo command #2: Authorize a paid user with a specific Dava/document limit (or unlimited)."""
    if not update.message or not update.effective_user:
        return
    if not access.is_owner(update.effective_user.id):
        await deny(update)
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "🏛️ *VAKIL AI ADMIN — PRO MEMBERSHIP PROVISIONING*\n"
            f"{DIVIDER}\n"
            "• 50 ड्राफ्ट प्लान: `/paid TELEGRAM_USER_ID 50`\n"
            "• 100 ड्राफ्ट प्लान: `/paid TELEGRAM_USER_ID 100`\n"
            "• अनलिमिटेड प्लान: `/paid TELEGRAM_USER_ID unlimited`",
            parse_mode="Markdown",
        )
        return
    target = context.args[0]
    if access.is_owner(target):
        await update.message.reply_text("👑 Chamber Admin के पास पहले से Unlimited विशेषाधिकार हैं।")
        return
    limit = int(os.getenv("DEFAULT_PAID_DOCUMENT_LIMIT", "50"))
    if len(context.args) >= 2:
        arg2 = context.args[1].strip().lower()
        if arg2 in {"unlimited", "inf", "all", "0", "-1"}:
            limit = -1
        elif arg2.isdigit() and int(arg2) > 0:
            limit = int(arg2)
        else:
            await update.message.reply_text(
                "⚠️ कृपया सही संख्या लिखें, जैसे: `/paid 987654321 50` या `/paid 987654321 unlimited`",
                parse_mode="Markdown",
            )
            return
    access.authorize_paid(target, update.effective_user.id, limit=limit)
    limit_str = "♾️ Unlimited Documents" if limit < 0 else f"`{limit}` Legal Documents (`0/{limit}` used)"
    await update.message.reply_text(
        "🏛️ *VAKIL AI ADMIN — PRO MEMBERSHIP ACTIVATED*\n"
        f"{DIVIDER}\n"
        f"👤 *Client ID:* `{target}`\n"
        f"🏷️ *Tier:* 💎 Vakil AI Pro Member\n"
        f"📊 *Allocated Quota:* {limit_str}\n"
        f"🟢 *Status:* Active & Verified",
        parse_mode="Markdown",
    )
    try:
        user_limit_str = "♾️ अनलिमिटेड (Unlimited)" if limit < 0 else f"`{limit}` विधिक ड्राफ्ट"
        await context.bot.send_message(
            chat_id=int(target),
            text=(
                "🏛️ *VAKIL DRAFTING AI — PRO MEMBERSHIP PASS*\n"
                f"{DIVIDER}\n"
                "💎 *आपकी Vakil AI Pro Membership सफलतापूर्वक सक्रिय कर दी गई है!*\n\n"
                f"• *सदस्यता स्तर:* 💎 Vakil AI Pro Member\n"
                f"• *ड्राफ्ट कोटा:* {user_limit_str}\n"
                "• *उपलब्ध सुविधाएं:* Voice Dictation • Document OCR • As-It-Is/Handwritten Legal Mode • Single-Page Recreation • Multi-Document Suite • DOCX & Court PDF\n"
                f"{DIVIDER}\n"
                "👉 ड्राफ्टिंग शुरू करने के लिए `/start` या `/newcase` दबाएँ तथा कोटा देखने के लिए `/myplan` भेजें।"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def myplan_command(update, context):
    """Show current user's subscription plan and remaining document quota."""
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    status = access.get_plan_status(uid)
    if not status["authorized"]:
        await update.message.reply_text(
            "🏛️ *VAKIL DRAFTING AI — MEMBERSHIP STATUS*\n"
            f"{DIVIDER}\n"
            f"👤 *Client ID:* `{uid}`\n"
            "🔴 *Status:* `Inactive / Not Registered`\n"
            f"{DIVIDER}\n"
            "🎁 *Trial Pass (5 Documents)* या *💎 Vakil AI Pro Plan* सक्रिय कराने के लिए एडमिन को अपनी Client ID भेजें।",
            parse_mode="Markdown",
        )
        return
    qlim = status["quota_limit"]
    used = status["used_documents"]
    rem = status["remaining"]
    bar = quota_progress_bar(used, qlim)
    quota_line = "♾️ Unlimited Access" if qlim < 0 else f"`{rem}` शेष (कुल `{qlim}` में से `{used}` प्रयुक्त)"
    await update.message.reply_text(
        "🏛️ *VAKIL DRAFTING AI — MEMBERSHIP PASS*\n"
        f"{DIVIDER}\n"
        f"👤 *Client ID:* `{uid}`\n"
        f"🏷️ *Active Tier:* {status['plan_label']}\n"
        f"📊 *Quota Meter:* `{bar}`\n"
        f"📑 *Available Balance:* {quota_line}\n"
        f"🟢 *Engine Status:* Priority Judicial Queue Active\n"
        f"{DIVIDER}\n"
        "_नोट: तैयार ड्राफ्ट में संशोधन (Edit / Add / Remove Point / Undo) करने पर कोई अतिरिक्त कोटा नहीं कटता है।_",
        parse_mode="Markdown",
    )


async def authorize(update, context):
    if not update.message or not update.effective_user:
        return
    if not access.is_owner(update.effective_user.id):
        await deny(update)
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "🏛️ *VAKIL AI ADMIN — AUTHORIZATION CONSOLE*\n"
            f"{DIVIDER}\n"
            "• `/trial USER_ID` — 5 ड्राफ्ट का Complimentary Trial Pass दें\n"
            "• `/paid USER_ID 50` — 50 ड्राफ्ट का Vakil AI Pro Plan दें\n"
            "• `/authorize USER_ID` — Enterprise Unlimited Access दें",
            parse_mode="Markdown",
        )
        return
    target = context.args[0]
    if access.is_owner(target):
        await update.message.reply_text("👑 Chamber Admin पहले से permanently authorized है।")
        return
    if len(context.args) >= 2 and context.args[1].lower() == "trial":
        lim = int(context.args[2]) if (len(context.args) >= 3 and context.args[2].isdigit()) else 5
        access.authorize_trial(target, update.effective_user.id, limit=lim)
        await update.message.reply_text(f"🎁 Client `{target}` को Trial Pass (`{lim}` documents) पर सक्रिय कर दिया गया है।", parse_mode="Markdown")
        return
    if len(context.args) >= 2 and context.args[1].lower() == "paid":
        lim = int(context.args[2]) if (len(context.args) >= 3 and context.args[2].isdigit()) else int(os.getenv("DEFAULT_PAID_DOCUMENT_LIMIT", "50"))
        access.authorize_paid(target, update.effective_user.id, limit=lim)
        await update.message.reply_text(f"💎 Client `{target}` को Vakil AI Pro Plan (`{lim}` documents) पर सक्रिय कर दिया गया है।", parse_mode="Markdown")
        return
    access.authorize(target, update.effective_user.id)
    await update.message.reply_text(f"✅ Client `{target}` को Enterprise Unlimited Plan पर सक्रिय कर दिया गया है।", parse_mode="Markdown")


async def unauthorize(update, context):
    if not update.message or not update.effective_user:
        return
    if not access.is_owner(update.effective_user.id):
        await deny(update)
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/unauthorize TELEGRAM_USER_ID`", parse_mode="Markdown")
        return
    target = context.args[0]
    if access.is_owner(target):
        await update.message.reply_text("🛡️ Chamber Admin को unauthorize नहीं किया जा सकता।")
        return
    removed = access.unauthorize(target)
    await update.message.reply_text(
        f"🏛️ *VAKIL AI ADMIN — ACCESS REVOKED*\n{DIVIDER}\n"
        f"{'✅ Client की सदस्यता समाप्त कर दी गई है।' if removed else 'ℹ️ यह Client सूची में नहीं था।'}\n"
        f"👤 *Client ID:* `{target}`",
        parse_mode="Markdown",
    )


async def authorized(update, context):
    if not update.message or not update.effective_user:
        return
    if not access.is_owner(update.effective_user.id):
        await deny(update)
        return
    users = access.authorized_users()
    lines = [
        "🏛️ *VAKIL AI ADMIN — SUBSCRIBER DIRECTORY*",
        DIVIDER,
        f"👑 *Chamber Admin (Sudo):* `{access.owner_id}` (♾️ Unlimited)",
        "",
        "📋 *Registered Clients & Quota Usage:*",
    ]
    if not users:
        lines.append("• _अभी कोई बाहरी क्लाइंट पंजीकृत नहीं है।_")
    for u in users:
        plan = str(u.get("plan", "unlimited")).lower()
        qlim = int(u.get("quota_limit", -1))
        used = int(u.get("used_documents", 0))
        badge = "🎁 Trial Pass" if plan == "trial" else ("💎 Pro Member" if plan == "paid" else "♾️ Unlimited")
        usage_str = "Unlimited" if qlim < 0 else f"{used}/{qlim} drafts used"
        lines.append(f"• `{u['user_id']}` — *{badge}* (`{usage_str}`)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def start(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    state = CaseState(
        case_id_for(uid),
        document_type="dava_plaint",
        style_mode="template_mode",
        style_selected=False,
        type_selected=False,
    )
    store.save(uid, state)
    await update.message.reply_text(
        STYLE_PROMPT_TEXT,
        parse_mode="Markdown",
        reply_markup=style_selection_menu(),
    )


async def newcase(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    state = CaseState(
        case_id_for(uid),
        document_type="dava_plaint",
        style_mode="template_mode",
        style_selected=False,
        type_selected=False,
    )
    store.save(uid, state)
    await update.message.reply_text(
        STYLE_PROMPT_TEXT,
        parse_mode="Markdown",
        reply_markup=style_selection_menu(),
    )


async def style_command(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    await update.message.reply_text(
        STYLE_PROMPT_TEXT,
        parse_mode="Markdown",
        reply_markup=style_selection_menu(),
    )


async def cancel(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    store.delete(case_id_for(update.effective_user.id))
    await update.message.reply_text(
        "🏛️ *VAKIL DRAFTING AI — SESSION CLOSED*\n"
        f"{DIVIDER}\n"
        "✅ वर्तमान ड्राफ्टिंग सेशन सुरक्षित रूप से समाप्त कर दिया गया है।\n"
        "👉 नया केस शुरू करने के लिए `/newcase` या `/start` दबाएँ।",
        parse_mode="Markdown",
    )


async def summary(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state:
        await update.message.reply_text(
            "🏛️ *VAKIL DRAFTING AI*\n"
            f"{DIVIDER}\n"
            "ℹ️ अभी कोई सक्रिय केस सेशन नहीं है। नया ड्राफ्ट शुरू करने के लिए `/newcase` दबाएँ।",
            parse_mode="Markdown",
            reply_markup=style_selection_menu(),
        )
        return
    await _safe_reply_text(update.message, summary_text(state), parse_mode="Markdown", reply_markup=case_actions(state))


async def download_current_draft(update, uid: int, state: CaseState, *, edit_message: bool = False):
    """Render and send the currently saved `state.draft` (including any edits) without regenerating or using quota."""
    target = update.callback_query.message if update.callback_query else update.message
    if not state or not state.draft:
        if edit_message and update.callback_query:
            await update.callback_query.edit_message_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है। कृपया पहले ड्राफ्ट तैयार करें।", reply_markup=case_actions(state) if state else None)
        else:
            await target.reply_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है। कृपया पहले ड्राफ्ट तैयार करें।")
        return

    status_msg = f"📥 *{DISPLAY_NAMES[state.document_type]} (v{state.draft_version})* की DOCX एवं PDF फ़ाइलें तैयार की जा रही हैं…"
    if edit_message and update.callback_query:
        await _safe_edit_text(update.callback_query, status_msg, parse_mode="Markdown")
    else:
        await _safe_reply_text(target, status_msg, parse_mode="Markdown")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            label = DISPLAY_NAMES[state.document_type].split(" /")[0].replace(" ", "_")
            base_name = f"{label}_{uid}_v{state.draft_version}"
            docx, pdf = await asyncio.to_thread(
                render_both,
                state.draft,
                tmp,
                base_name,
                os.getenv("LEGAL_PAPER", "legal"),
            )
            with open(docx, "rb") as f:
                await target.reply_document(f, filename=docx.name, caption=f"📄 Editable Word (DOCX) — {DISPLAY_NAMES[state.document_type]} (v{state.draft_version})")
            with open(pdf, "rb") as f:
                await target.reply_document(f, filename=pdf.name, caption=f"📑 Court-Ready PDF — {DISPLAY_NAMES[state.document_type]} (v{state.draft_version})")
        await _safe_reply_text(
            target,
            f"✅ *{DISPLAY_NAMES[state.document_type]} (v{state.draft_version})* सफलतापूर्वक भेज दिया गया है।",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
    except Exception as exc:
        log.exception("download_current_draft failed")
        await target.reply_text(f"❌ फ़ाइल रेंडर नहीं हो सकी: {str(exc)[:300]}")


async def download_command(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    state = get_state(uid)
    await download_current_draft(update, uid, state, edit_message=False)


async def intake_text(update, context, text=None, *, suppress_questions=False, announce=True, split_parts_count: int = 1):
    if not update.effective_user:
        return
    if not access.is_authorized(update.effective_user.id):
        await deny(update)
        return
    uid = update.effective_user.id
    text = decode_krutidev_hybrid((text or update.message.text or "").strip())
    state = get_state(uid)
    auto_sp, sp_page_no, sp_inst = detect_single_page_intent(text)
    auto_as_is = detect_as_is_intent(text)
    if not state:
        inferred_type = infer_document_type(text, default="dava_plaint")
        initial_style = "single_page_mode" if auto_sp else ("as_is_mode" if auto_as_is else "template_mode")
        state = CaseState(
            case_id_for(uid),
            document_type=inferred_type,
            style_mode=initial_style,
            style_selected=bool(auto_sp or auto_as_is),
            type_selected=False,
            single_page_mode=auto_sp,
            target_page_number=sp_page_no if auto_sp else 0,
            page_correction_instruction=sp_inst if auto_sp else "",
        )
        if auto_sp:
            state.facts["target_page_number"] = sp_page_no
            state.facts["page_correction_instruction"] = sp_inst
        store.save(uid, state)
    else:
        if auto_sp:
            state.style_mode = "single_page_mode"
            state.style_selected = True
            state.single_page_mode = True
            state.target_page_number = sp_page_no
            state.page_correction_instruction = sp_inst
            state.facts["target_page_number"] = sp_page_no
            state.facts["page_correction_instruction"] = sp_inst
        elif auto_as_is and state.style_mode != "single_page_mode":
            state.style_mode = "as_is_mode"
            state.style_selected = True
        elif (state.style_mode == "single_page_mode" or getattr(state, "single_page_mode", False)) and detect_edit_intent(text) and state.facts.get("raw_source_text"):
            # User already uploaded the page and is now sending the correction instruction for that single page
            state.page_correction_instruction = text
            state.facts["page_correction_instruction"] = text
            store.save(uid, state)
            if not suppress_questions:
                await _safe_reply_text(
                    update.message,
                    f"✅ *एकल पन्ना संशोधन निर्देश दर्ज कर लिया गया है:*\n`{text}`\n\n{questions_text(state)}",
                    parse_mode="Markdown",
                    reply_markup=case_actions(state),
                )
            return {"status": "ready", "facts": state.facts}
        if not state.facts and not getattr(state, "type_selected", False):
            inferred_type = infer_document_type(text, default=state.document_type)
            state.document_type = inferred_type

    state.record("user", text)
    if announce:
        if state.style_mode == "single_page_mode" or getattr(state, "single_page_mode", False):
            pg_num = max(1, int(getattr(state, "target_page_number", 0) or state.facts.get("target_page_number") or 1))
            mode_note = f" (📃 एकल पन्ना मोड — पृष्ठ {pg_num})"
        elif state.style_mode == "as_is_mode":
            mode_note = " (📄 हूबहू / हस्तलिखित लीगल मोड)"
        else:
            mode_note = ""
        parts_note = f" _(विस्तृत पाठ के `{split_parts_count}` भाग संयुक्त किए गए)_" if split_parts_count > 1 else ""
        await _safe_reply_text(
            update.message,
            f"⚙️ *{DISPLAY_NAMES[state.document_type]}*{mode_note}{parts_note} — आपके विवरण का विधिक विश्लेषण किया जा रहा है…",
            parse_mode="Markdown",
        )
    try:
        result = await asyncio.to_thread(orchestrator.extract_and_collect, state, text)
        store.save(uid, state)
    except Exception:
        log.exception("Intake failed")
        if state.style_mode in {"as_is_mode", "single_page_mode"}:
            store.save(uid, state)
            result = {"status": "ready", "facts": state.facts}
        else:
            await update.message.reply_text("❌ जानकारी प्रोसेस नहीं हो सकी। कृपया दोबारा भेजें।")
            return
    if suppress_questions:
        return result

    if not state.style_selected:
        await _safe_reply_text(
            update.message,
            f"✅ *विवरण सफलतापूर्वक दर्ज कर लिया गया है!*\n\n{summary_text(state)}\n"
            "👇 *कृपया चुनें कि आप किस ड्राफ्टिंग शैली में दस्तावेज़ तैयार करना चाहते हैं:*",
            parse_mode="Markdown",
            reply_markup=style_selection_menu(),
        )
        return result

    if result["status"] == "needs_information":
        await _safe_reply_text(
            update.message,
            f"{questions_text(state)}\n\n📊 *पूर्णता (Readiness):* `{progress(state)}`",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
        return result
    await _safe_reply_text(
        update.message,
        f"✅ *सभी आवश्यक तथ्य प्राप्त हो गए हैं।*\n\n{summary_text(state)}",
        parse_mode="Markdown",
        reply_markup=case_actions(state),
    )
    return result


async def start_additional_document(update, context, document_type: str):
    uid = update.effective_user.id
    state = get_state(uid)
    if not state or not state.facts:
        await update.message.reply_text("कृपया पहले एक केस तैयार करें, उसके बाद उसमें अगला दस्तावेज़ जोड़ें।", reply_markup=style_selection_menu())
        return
    if state.document_type == document_type and state.draft:
        await update.message.reply_text("यह दस्तावेज़ अभी सक्रिय है। अलग दस्तावेज़ बनाने के लिए नीचे से दूसरा प्रकार चुनें।", reply_markup=document_menu(state))
        return
    state.begin_new_document(document_type)
    try:
        await asyncio.to_thread(orchestrator.refresh_intake, state)
        store.save(uid, state)
    except Exception:
        log.exception("additional document intake reconciliation failed")
    missing = missing_fields(state.facts, document_type, style_mode=state.style_mode)
    if missing:
        msg = (
            f"📑 *नया सह-दस्तावेज़: {DISPLAY_NAMES[document_type]}*\n"
            f"{DIVIDER}\n"
            "यह दस्तावेज़ आपके वर्तमान केस के तथ्यों से तैयार किया जाएगा।\n\n"
            f"{questions_text(state)}"
        )
        await _safe_reply_text(update.message, msg, parse_mode="Markdown", reply_markup=case_actions(state))
        return
    await update.message.reply_text(
        f"⚙️ *{DISPLAY_NAMES[document_type]}* आपके केस के तथ्यों से तैयार किया जा रहा है…",
        parse_mode="Markdown",
    )
    await generate_for(update, uid, state, edit=False)


async def add_document_command(update, context):
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state or not state.facts:
        await update.message.reply_text("कृपया पहले `/newcase` से एक केस प्रारंभ करें।", parse_mode="Markdown", reply_markup=style_selection_menu())
        return
    await update.message.reply_text(
        "🏛️ *VAKIL DRAFTING AI — MULTI-DOCUMENT SUITE*\n"
        f"{DIVIDER}\n"
        "📑 *इसी केस के तथ्यों के आधार पर अगला विधिक दस्तावेज़ चुनें:*",
        parse_mode="Markdown",
        reply_markup=document_menu(state),
    )


async def text_message(update, context):
    if not update.effective_user or not update.message:
        return
    uid = update.effective_user.id
    raw_text = update.message.text or ""
    state = get_state(uid)

    # Check if the user is explicitly asking to recreate a single page ("पेज 3 में नाम सही करके सिर्फ पेज 3 बना दो")
    is_sp, sp_pg, sp_inst = detect_single_page_intent(raw_text)
    if is_sp and state and (state.facts.get("raw_source_pages") or state.facts.get("raw_source_text") or state.draft):
        state.style_mode = "single_page_mode"
        state.single_page_mode = True
        state.target_page_number = sp_pg
        state.page_correction_instruction = sp_inst
        state.facts["target_page_number"] = sp_pg
        state.facts["page_correction_instruction"] = sp_inst
        store.save(uid, state)
        await generate_for(update, uid, state, edit=False)
        return

    if state and state.draft:
        requested_type = detect_additional_document_type(raw_text)
        if requested_type:
            await start_additional_document(update, context, requested_type)
            return
        if state.pending_point_action or state.edit_mode or detect_edit_intent(raw_text):
            state.edit_mode = True
            store.save(uid, state)
            await process_edit_instruction(update, context, raw_text)
            return

    # Buffer and reassemble multi-part messages when Telegram automatically splits a long paste (>4096 chars)
    buf = _pending_text_buffers.get(uid)
    if buf is not None:
        buf.append(raw_text)
        return

    _pending_text_buffers[uid] = [raw_text]
    # Wait briefly if the message is long or looks like a multi-part paste so all parts arrive in order
    wait_sec = 0.9 if len(raw_text) >= 1200 else 0.35
    await asyncio.sleep(wait_sec)
    while True:
        cur_len = len(_pending_text_buffers.get(uid, []))
        if cur_len <= 1:
            break
        await asyncio.sleep(0.35)
        if len(_pending_text_buffers.get(uid, [])) == cur_len:
            break

    parts = _pending_text_buffers.pop(uid, [raw_text])
    combined_text = "\n".join(p.strip() for p in parts if p and p.strip())

    async with _get_user_lock(uid):
        await intake_text(update, context, combined_text, split_parts_count=len(parts))


async def voice_message(update, context):
    if not update.message or not update.message.voice or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    state = get_state(uid)

    await update.message.reply_text("🎙️ *Voice Dictation प्राप्त हुआ।* ऑडियो को विधिक पाठ में बदला जा रहा है…", parse_mode="Markdown")
    try:
        f = await context.bot.get_file(update.message.voice.file_id)
        buf = BytesIO()
        await f.download_to_memory(out=buf)
        transcript = await asyncio.to_thread(transcribe_voice, buf.getvalue(), "telegram_voice.ogg")
        await _safe_reply_text(
            update.message,
            f"🎙️ *Voice Transcript:*\n{DIVIDER}\n{transcript[:3500]}",
            parse_mode="Markdown",
        )

        state = get_state(uid)
        is_sp, sp_pg, sp_inst = detect_single_page_intent(transcript)
        if is_sp and state and (state.facts.get("raw_source_pages") or state.facts.get("raw_source_text") or state.draft):
            state.style_mode = "single_page_mode"
            state.single_page_mode = True
            state.target_page_number = sp_pg
            state.page_correction_instruction = sp_inst
            state.facts["target_page_number"] = sp_pg
            state.facts["page_correction_instruction"] = sp_inst
            store.save(uid, state)
            await generate_for(update, uid, state, edit=False)
            return

        if state and state.draft:
            requested_type = detect_additional_document_type(transcript)
            if requested_type:
                await start_additional_document(update, context, requested_type)
                return
            if state.pending_point_action or state.edit_mode or detect_edit_intent(transcript):
                state.edit_mode = True
                store.save(uid, state)
                await process_edit_instruction(update, context, transcript)
                return

        async with _get_user_lock(uid):
            await intake_text(update, context, transcript)
    except Exception:
        log.exception("voice failed")
        await update.message.reply_text("❌ Voice note प्रोसेस नहीं हो सका। कृपया पुनः प्रयास करें।")


async def image_document_message(update, context):
    """OCR a Telegram photo/image/PDF/DOCX/TXT and feed extracted text + per-page slices + caption into semantic intake, As-It-Is mode, Single-Page mode, or edit mode."""
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    caption = (update.message.caption or "").strip()
    auto_sp, sp_pg, sp_inst = detect_single_page_intent(caption)
    auto_as_is = detect_as_is_intent(caption)

    async with _get_user_lock(uid):
        state = get_state(uid)
        if not state:
            inferred = infer_document_type(caption, default="dava_plaint")
            init_style = "single_page_mode" if auto_sp else ("as_is_mode" if auto_as_is else "template_mode")
            state = CaseState(
                case_id_for(uid),
                document_type=inferred,
                style_mode=init_style,
                style_selected=bool(auto_sp or auto_as_is),
                type_selected=False,
                single_page_mode=auto_sp,
                target_page_number=sp_pg if auto_sp else 0,
                page_correction_instruction=sp_inst if auto_sp else "",
            )
            store.save(uid, state)
        else:
            if auto_sp:
                state.style_mode = "single_page_mode"
                state.style_selected = True
                state.single_page_mode = True
                state.target_page_number = sp_pg
                state.page_correction_instruction = sp_inst
                state.facts["target_page_number"] = sp_pg
                state.facts["page_correction_instruction"] = sp_inst
                store.save(uid, state)
            elif auto_as_is and state.style_mode != "single_page_mode":
                state.style_mode = "as_is_mode"
                state.style_selected = True
                store.save(uid, state)

        file_bytes = None
        content_type = None
        filename = "telegram_document"
        try:
            if update.message.photo:
                photo = update.message.photo[-1]
                tg_file = await context.bot.get_file(photo.file_id)
                buf = BytesIO()
                await tg_file.download_to_memory(out=buf)
                file_bytes = buf.getvalue()
                content_type = "image/jpeg"
                filename = "telegram_photo.jpg"
            elif update.message.document:
                doc = update.message.document
                content_type = (doc.mime_type or "application/octet-stream").lower()
                filename = doc.file_name or filename
                supported = (
                    content_type.startswith("image/")
                    or content_type in {"application/pdf", "text/plain", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
                    or filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".heif", ".pdf", ".docx", ".txt"))
                )
                if not supported:
                    await update.message.reply_text(
                        "⚠️ केवल Image फ़ाइलें (JPG/PNG/TIFF/HEIF), PDF दस्तावेज़, और Word (.docx) / Text (.txt) फ़ाइलें समर्थित हैं।"
                    )
                    return
                tg_file = await context.bot.get_file(doc.file_id)
                buf = BytesIO()
                await tg_file.download_to_memory(out=buf)
                file_bytes = buf.getvalue()
                if content_type == "application/octet-stream":
                    lower = filename.lower()
                    if lower.endswith(".pdf"):
                        content_type = "application/pdf"
                    elif lower.endswith(".docx"):
                        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    elif lower.endswith(".txt"):
                        content_type = "text/plain"
                    elif lower.endswith(".png"):
                        content_type = "image/png"
                    elif lower.endswith((".jpg", ".jpeg")):
                        content_type = "image/jpeg"
                    elif lower.endswith((".tif", ".tiff")):
                        content_type = "image/tiff"
                    elif lower.endswith(".heif"):
                        content_type = "image/heif"
            else:
                return

            await update.message.reply_text("📸 *Judicial OCR & Document Scanner सक्रिय है…* दस्तावेज़ से पाठ और संरचना निकाली जा रही है…", parse_mode="Markdown")
            extracted = await asyncio.to_thread(analyze_document_bytes, file_bytes, content_type, filename)
            text = extracted.text.strip()

            # Store per-page text slices if this is a PDF so Single-Page Recreation (`/page 3`) can target any exact page
            if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
                try:
                    pdf_pages = await asyncio.to_thread(extract_pdf_pages_list, file_bytes)
                    if pdf_pages:
                        state.facts["raw_source_pages"] = pdf_pages
                        store.save(uid, state)
                except Exception:
                    pass

            if caption and not detect_as_is_intent(caption) and not auto_sp:
                text = f"{caption}\n\n{text}".strip()
            if not text:
                await update.message.reply_text("⚠️ दस्तावेज़ से पठनीय पाठ नहीं मिला। कृपया साफ़ फोटो या PDF भेजें।")
                return

            if state.draft and not auto_sp and (state.pending_point_action or state.edit_mode or (caption and detect_edit_intent(caption)) or detect_edit_intent(text[:500])):
                state.edit_mode = True
                store.save(uid, state)
                await process_document_sansodhan(update, context, ocr_text=text, caption=caption, model_id=extracted.model_id)
                return

            if not state.type_selected:
                inferred_doc_type = infer_document_type(f"{caption}\n{text[:1500]}", default=state.document_type)
                state.document_type = inferred_doc_type
                store.save(uid, state)

            page_count = extracted.page_count
            chunk_size = max(50000, int(os.getenv("IMAGE_INTAKE_CHUNK_CHARS", "120000")))
            chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
            if len(chunks) == 1:
                await update.message.reply_text(
                    f"✅ *स्कैन पूर्ण* — `{page_count}` पृष्ठ (`{len(text)}` अक्षर)।\n"
                    "🧠 दस्तावेज़ से विधिक तथ्य एवं संरचना संकलित की जा रही है…",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"✅ *स्कैन पूर्ण* — `{page_count}` पृष्ठ (`{len(text)}` अक्षर)।\n"
                    f"📚 विस्तृत दस्तावेज़ को `{len(chunks)}` भागों में विश्लेषित किया जा रहा है…",
                    parse_mode="Markdown",
                )
            for index, chunk in enumerate(chunks, 1):
                if len(chunks) > 1 and index in {1, len(chunks)}:
                    await update.message.reply_text(f"⚙️ विधिक तथ्य विश्लेषण ({index}/{len(chunks)})…")
                await intake_text(update, context, chunk, suppress_questions=True, announce=False)

            state = get_state(uid)
            if state and (state.style_mode == "single_page_mode" or getattr(state, "single_page_mode", False)):
                pg_num = max(1, int(getattr(state, "target_page_number", 0) or state.facts.get("target_page_number") or 1))
                await _safe_reply_text(
                    update.message,
                    f"✅ *एकल पन्ना (पृष्ठ {pg_num}) सफलतापूर्वक पढ़ लिया गया है!*\n\n{questions_text(state)}",
                    parse_mode="Markdown",
                    reply_markup=case_actions(state),
                )
            elif state and state.style_mode == "as_is_mode":
                await _safe_reply_text(
                    update.message,
                    f"✅ *दस्तावेज़ सफलतापूर्वक पढ़ लिया गया है!*\n\n{summary_text(state)}\n"
                    "💡 *अगला कदम:*\n"
                    "• यदि इस दस्तावेज़ का कोई अगला पन्ना (Page 2, Page 3...) बाकी है, तो उसकी Photo अभी भेजें।\n"
                    "• यदि सभी पन्ने आ गए हैं, तो नीचे **⚡ हूबहू लीगल ड्राफ्ट तैयार करें (Generate Legal)** दबाएं!",
                    parse_mode="Markdown",
                    reply_markup=case_actions(state),
                )
            elif state and not state.style_selected:
                await _safe_reply_text(
                    update.message,
                    f"✅ *दस्तावेज़ से सभी प्रमुख तथ्य संकलित कर लिए गए हैं!*\n\n{summary_text(state)}\n"
                    "👇 *कृपया चुनें कि आप इसे किस रूप में तैयार करना चाहते हैं (हूबहू लीगल पेपर के लिए 4था विकल्प या केवल 1 पन्ना री-क्रिएट करने के लिए 5वां विकल्प चुनें):*",
                    parse_mode="Markdown",
                    reply_markup=style_selection_menu(),
                )
            elif state and not missing_fields(state.facts, state.document_type, style_mode=state.style_mode):
                await _safe_reply_text(
                    update.message,
                    f"✅ *दस्तावेज़ से सभी आवश्यक तथ्य संकलित हो गए हैं।*\n\n{summary_text(state)}",
                    parse_mode="Markdown",
                    reply_markup=case_actions(state),
                )
            else:
                await _safe_reply_text(
                    update.message,
                    f"📋 *OCR विश्लेषण पूर्ण।* कृपया शेष आवश्यक विवरण प्रदान करें (या सीधे हूबहू लीगल पेपर में बनाने के लिए **📄 हूबहू लीगल पेपर में बनाएं** दबाएं):\n\n{questions_text(state)}\n\n📊 *पूर्णता:* `{progress(state)}`",
                    parse_mode="Markdown",
                    reply_markup=case_actions(state),
                )
        except DocumentIntelligenceError as exc:
            log.exception("Document Intelligence failed")
            await update.message.reply_text(f"❌ दस्तावेज़ पढ़ा नहीं जा सका: {str(exc)[:500]}")
        except Exception:
            log.exception("image/document processing failed")
            await update.message.reply_text("❌ दस्तावेज़ प्रोसेस नहीं हो सका। कृपया दोबारा भेजें।")


async def generate_for(update, uid, state, edit=False):
    target = update.callback_query.message if update.callback_query else update.message
    allowed, quota_msg = access.can_generate(uid)
    if not allowed:
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(quota_msg, parse_mode="Markdown")
        else:
            await target.reply_text(quota_msg, parse_mode="Markdown")
        return
    style_label = STYLE_NAMES.get(state.style_mode, state.style_mode)
    if state.style_mode == "single_page_mode" or getattr(state, "single_page_mode", False):
        pg_num = max(1, int(getattr(state, "target_page_number", 0) or state.facts.get("target_page_number") or 1))
        step2_text = f"☑️ `2/4` पृष्ठ {pg_num} की मूल पैरा संख्या एवं संरचना में लक्षित सुधार"
    elif state.style_mode == "as_is_mode":
        step2_text = "☑️ `2/4` मूल/हस्तलिखित दस्तावेज़ को उचित विधिक ढांचे में व्यवस्थित करना"
    else:
        step2_text = "☑️ `2/4` उपयुक्त न्यायिक प्रारूप (Blueprint) का चयन"
    status_card = (
        f"🏛️ *VAKIL DRAFTING AI — DRAFTING ENGINE*\n"
        f"{DIVIDER}\n"
        f"📄 *दस्तावेज़:* {DISPLAY_NAMES[state.document_type]}\n"
        f"🎨 *शैली:* {style_label}\n"
        f"{DIVIDER}\n"
        "⚙️ *विधिक ड्राफ्ट तैयार किया जा रहा है…*\n\n"
        "☑️ `1/4` पक्षकार एवं केस तथ्यों का सत्यापन\n"
        f"{step2_text}\n"
        "🔄 `3/4` द्विभाषी अंकों (`अंक + शब्द`) एवं शुद्ध कोर्ट भाषा का समायोजन\n"
        "⏳ `4/4` कोर्ट-मानक Legal Size DOCX एवं PDF का निर्माण"
    )
    if edit:
        await _safe_edit_text(update.callback_query, status_card, parse_mode="Markdown")
    else:
        await _safe_reply_text(target, status_card, parse_mode="Markdown")
    try:
        draft = await asyncio.to_thread(orchestrator.draft_live, state)
        state.set_draft(draft)
        with tempfile.TemporaryDirectory() as tmp:
            label = DISPLAY_NAMES[state.document_type].split(" /")[0].replace(" ", "_")
            sp_meta = (draft.get("layout") or {})
            if sp_meta.get("single_page_mode"):
                pg_num = sp_meta.get("single_page_number", 1)
                base_file = f"{label}_Page_{pg_num}_{uid}"
            else:
                base_file = f"{label}_{uid}"
            docx, pdf = await asyncio.to_thread(render_both, draft, tmp, base_file, os.getenv("LEGAL_PAPER", "legal"))
            with open(docx, "rb") as f:
                await target.reply_document(f, filename=docx.name, caption=f"📄 Editable Legal Word (DOCX) — {DISPLAY_NAMES[state.document_type]}")
            with open(pdf, "rb") as f:
                await target.reply_document(f, filename=pdf.name, caption=f"📑 Court-Ready Legal PDF — {DISPLAY_NAMES[state.document_type]}")
        store.save(uid, state)
        plan_status = access.consume_quota(uid)
        if plan_status.get("quota_limit", -1) > 0:
            quota_info = f"📊 *शेष कोटा (Quota):* `{plan_status['remaining']}/{plan_status['quota_limit']}` ड्राफ्ट शेष ({plan_status['plan_label']})\n"
        else:
            quota_info = f"📊 *सदस्यता:* {plan_status['plan_label']}\n"
        bp_label = format_public_blueprint_name(state.selected_template_name, state.document_type, state.style_mode)
        await _safe_reply_text(
            target,
            f"🏛️ *VAKIL DRAFTING AI — DRAFT COMPLETED*\n"
            f"{DIVIDER}\n"
            f"✅ *{DISPLAY_NAMES[state.document_type]} (Legal Size) सफलतापूर्वक तैयार है!*\n"
            f"🎨 *शैली:* {style_label}\n"
            f"📘 *प्रारूप:* `{bp_label}`\n"
            f"{quota_info}"
            f"{DIVIDER}\n"
            "💡 *अगले विकल्प (निःशुल्क स्मार्ट संशोधन एवं टूल्स):*\n"
            "• किसी खास पैरा/हेडिंग या पूरे ड्राफ्ट में बदलाव हेतु *✏️ स्मार्ट संशोधन* दबाएं (या सीधे लिखकर/बोलकर भेजें)\n"
            "• सभी पैराग्राफ व हेडिंग देखने हेतु *📑 सभी पैरा व हेडिंग देखें* दबाएं\n"
            "• खाली स्थान (`....`) व त्रुटि जांचने हेतु *🔍 त्रुटि जांच (Audit)* दबाएं\n"
            "• किसी एक पन्ने (Page 1, 2, 3...) को अलग निकालने या सुधारने हेतु *📃 एकल पन्ना निकालें* दबाएं!",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
    except Exception as exc:
        log.exception("generation failed")
        msg = "⏳ AI सर्वर अभी व्यस्त है। कृपया कुछ क्षण बाद पुनः प्रयास करें।" if isinstance(exc, GeminiError) and exc.retryable else f"❌ ड्राफ्ट तैयार नहीं हो सका: {str(exc)[:500]}"
        await target.reply_text(msg)


async def enter_edit_mode(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state or not state.draft:
        await update.message.reply_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है। कृपया पहले ड्राफ्ट तैयार करें।")
        return
    state.edit_mode = True
    state.pending_point_action = ""
    state.pending_edit = {}
    store.save(update.effective_user.id, state)
    await update.message.reply_text(
        "🏛️ *VAKIL DRAFTING AI — SCOPE-AWARE SMART EDIT STUDIO*\n"
        f"{DIVIDER}\n"
        "✏️ अपना संशोधन **Text ✍️ लिखकर**, **Voice Note 🎙️ बोलकर**, या **संशोधन पन्ने की Photo / PDF / DOCX 📸** भेजकर बताएं (Document Intelligence + GPT-6.0-Luna इंजन सक्रिय है)।\n\n"
        "🎯 *1. केवल किसी विशेष पैरा या हेडिंग में बदलाव (Targeted Scope):*\n"
        "• `सिर्फ पैरा 3 में वर्ष 2024 के स्थान पर 2025 कर दो (बाकी जगह मत बदलना)।`\n"
        "• `शीर्षक में स्थायी निषेधाज्ञा के साथ बंटवारा भी जोड़ दो।`\n"
        "• `प्रार्थना के बिंदु (ख) में ... सुधार कर दो।`\n\n"
        "🌐 *2. पूरे ड्राफ्ट में हर जगह बदलाव (Global Scope):*\n"
        "• `पूरे ड्राफ्ट में सभी जगह गाटा संख्या 412 के स्थान पर 415 कर दो।`\n"
        "• `हर जगह वादी का नाम राम कुमार के स्थान पर राम कुमार शर्मा कर दो।`\n\n"
        "💡 _Preview कार्ड पर आप 1-टैप बटन से भी चुन सकेंगे कि बदलाव **केवल उसी पैरा में** करना है या **पूरे ड्राफ्ट में सभी जगह**!_",
        parse_mode="Markdown",
    )


async def edit_command(update, context):
    await enter_edit_mode(update, context)


async def process_edit_instruction(update, context, instruction: str):
    uid = update.effective_user.id
    if not access.is_authorized(uid):
        await deny(update)
        return
    state = get_state(uid)
    if not state or not state.draft:
        await update.message.reply_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है। पहले ड्राफ्ट जनरेट करें।")
        return
    instruction = decode_krutidev_hybrid(instruction.strip())
    if not instruction:
        return
    state.edit_mode = True
    action_mode = state.pending_point_action
    state.pending_point_action = ""
    await update.message.reply_text("⚙️ *आपके निर्देश के अनुसार स्मार्ट विधिक संशोधन तैयार किया जा रहा है…*", parse_mode="Markdown")
    try:
        if action_mode == "add":
            edit = await asyncio.to_thread(editor.parse_add_point, state.draft, instruction)
        elif action_mode == "remove":
            edit = await asyncio.to_thread(editor.parse_remove_point, state.draft, instruction)
        else:
            edit = await asyncio.to_thread(editor.parse, state.draft, instruction)

        candidate, preview = await asyncio.to_thread(editor.apply, state.draft, edit)
        state.pending_edit = {"edit": edit, "candidate": candidate, "instruction": instruction}
        store.save(uid, state)
        await _safe_reply_text(
            update.message,
            "🏛️ *VAKIL DRAFTING AI — CHANGE PREVIEW*\n"
            f"{DIVIDER}\n"
            f"{preview}\n"
            f"{DIVIDER}\n"
            "👇 *क्या आप इस संशोधन को ड्राफ्ट में लागू करना चाहते हैं?*\n"
            "_(यदि आप इस बदलाव का दायरा 'केवल एक स्थान' या 'पूरे ड्राफ्ट में सभी जगह' बदलना चाहते हैं, तो नीचे Scope बटन दबाएं)_",
            parse_mode="Markdown",
            reply_markup=edit_actions(state),
        )
    except Exception as exc:
        log.exception("edit parse/apply failed")
        await update.message.reply_text(f"⚠️ संशोधन लागू करने हेतु सटीक पैराग्राफ स्पष्ट नहीं हो सका। कृपया पैराग्राफ संख्या या शब्द स्पष्ट लिखें।\n\n{str(exc)[:400]}")


async def process_document_sansodhan(update, context, ocr_text: str, caption: str = "", model_id: str = ""):
    """Apply Document Intelligence OCR + GPT-6.0-Luna precision sansodhan when a user uploads a Photo/PDF/DOCX while editing a draft."""
    uid = update.effective_user.id
    if not access.is_authorized(uid):
        await deny(update)
        return
    state = get_state(uid)
    if not state or not state.draft:
        await update.message.reply_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है। पहले ड्राफ्ट जनरेट करें।")
        return
    ocr_clean = decode_krutidev_hybrid((ocr_text or "").strip())
    caption_clean = decode_krutidev_hybrid((caption or "").strip())
    if not ocr_clean and not caption_clean:
        return
    state.edit_mode = True
    action_mode = state.pending_point_action
    state.pending_point_action = ""
    await update.message.reply_text(
        "🧠 *Document Intelligence + GPT-6.0-Luna Sansodhan सक्रिय है…*\n"
        "दस्तावेज़ से प्राप्त तथ्यों के अनुसार आपके ड्राफ्ट में विधिक संशोधन तैयार किया जा रहा है…",
        parse_mode="Markdown",
    )
    try:
        if action_mode == "add":
            combined_inst = f"{caption_clean}\n\n{ocr_clean}".strip() if caption_clean else ocr_clean
            edit = await asyncio.to_thread(editor.parse_add_point, state.draft, combined_inst)
        elif action_mode == "remove":
            combined_inst = f"{caption_clean}\n\n{ocr_clean}".strip() if caption_clean else ocr_clean
            edit = await asyncio.to_thread(editor.parse_remove_point, state.draft, combined_inst)
        else:
            edit = await asyncio.to_thread(editor.parse_document_sansodhan, state.draft, ocr_clean, caption_clean)

        candidate, preview = await asyncio.to_thread(editor.apply, state.draft, edit)
        state.pending_edit = {
            "edit": edit,
            "candidate": candidate,
            "instruction": caption_clean or ocr_clean[:300],
        }
        store.save(uid, state)
        await _safe_reply_text(
            update.message,
            "🏛️ *VAKIL DRAFTING AI — DOCUMENT INTELLIGENCE SANSODHAN PREVIEW*\n"
            f"{DIVIDER}\n"
            f"{preview}\n"
            f"{DIVIDER}\n"
            "👇 *क्या आप इस संशोधन को ड्राफ्ट में लागू करना चाहते हैं?*",
            parse_mode="Markdown",
            reply_markup=edit_actions(state),
        )
    except Exception as exc:
        log.exception("document sansodhan failed")
        await update.message.reply_text(
            f"⚠️ दस्तावेज़ से संशोधन लागू करने में त्रुटि हुई। कृपया स्पष्ट निर्देश लिखें।\n\n{str(exc)[:400]}"
        )


async def edit_text_message(update, context):
    await process_edit_instruction(update, context, update.message.text)


async def edit_voice_message(update, context):
    if not update.message or not update.message.voice or not update.effective_user:
        return
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state or not state.draft:
        await update.message.reply_text("कृपया पहले एक ड्राफ्ट तैयार करें।")
        return
    await update.message.reply_text("🎙️ *Voice Edit प्राप्त हुआ।* ट्रांसक्रिप्ट तैयार किया जा रहा है…", parse_mode="Markdown")
    try:
        f = await context.bot.get_file(update.message.voice.file_id)
        buf = BytesIO()
        await f.download_to_memory(out=buf)
        transcript = await asyncio.to_thread(transcribe_voice, buf.getvalue(), "telegram_edit.ogg")
        await _safe_reply_text(update.message, f"📝 *Voice Instruction:*\n{transcript[:3500]}", parse_mode="Markdown")
        await process_edit_instruction(update, context, transcript)
    except Exception:
        log.exception("voice edit failed")
        await update.message.reply_text("❌ Voice edit प्रोसेस नहीं हो सका।")


async def apply_edit(update, context):
    q = update.callback_query
    uid = q.from_user.id
    if not access.is_authorized(uid):
        await q.edit_message_text("🔒 यह सेवा केवल अधिकृत सदस्यों के लिए है।")
        return
    state = get_state(uid)
    pending = state.pending_edit if state else {}
    candidate = pending.get("candidate") if pending else None
    if not state or not candidate:
        await q.edit_message_text("⚠️ यह संशोधन अब उपलब्ध नहीं है।")
        return

    state.set_draft(candidate)
    state.edit_mode = True
    store.save(uid, state)

    await _safe_edit_text(q, "⚙️ *संशोधन लागू किया जा रहा है!* अपडेटेड DOCX एवं PDF तैयार किए जा रहे हैं…", parse_mode="Markdown")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            label = DISPLAY_NAMES[state.document_type].split(" /")[0].replace(" ", "_")
            base_name = f"{label}_{uid}_v{state.draft_version}"
            docx, pdf = await asyncio.to_thread(
                render_both,
                candidate,
                tmp,
                base_name,
                os.getenv("LEGAL_PAPER", "legal"),
            )
            with open(docx, "rb") as f:
                await q.message.reply_document(f, filename=docx.name, caption=f"📄 Updated Word (DOCX) — v{state.draft_version}")
            with open(pdf, "rb") as f:
                await q.message.reply_document(f, filename=pdf.name, caption=f"📑 Updated Court PDF — v{state.draft_version}")
        await _safe_reply_text(
            q.message,
            f"🏛️ *VAKIL DRAFTING AI — REVISION v{state.draft_version} COMPLETE*\n"
            f"{DIVIDER}\n"
            "✅ आपका संशोधन सफलतापूर्वक लागू कर दिया गया है (कोई कोटा नहीं काटा गया)।\n"
            "• यदि आप इस बदलाव को वापस लेना चाहते हैं, तो नीचे **⏪ पूर्ववत करें (Undo)** दबाएं।",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
    except Exception:
        log.exception("render after edit failed")
        await q.message.reply_text(
            "❌ संशोधन सुरक्षित हो गया है, किंतु फ़ाइल रेंडर नहीं हो सकी। नीचे '📥 DOCX व PDF डाउनलोड करें' बटन दबाकर या `/download` भेजकर फ़ाइल प्राप्त करें।",
            reply_markup=case_actions(state),
        )


async def button(update, context):
    q = update.callback_query
    await q.answer()
    if not access.is_authorized(q.from_user.id):
        await q.edit_message_text("🔒 यह एक प्राइवेट लीगल ड्राफ्टिंग सुइट है। `/myid` भेजकर अपनी Client ID प्राप्त करें।")
        return
    uid = q.from_user.id
    cid = case_id_for(uid)

    if q.data == "choose_style":
        await _safe_edit_text(
            q,
            STYLE_PROMPT_TEXT,
            parse_mode="Markdown",
            reply_markup=style_selection_menu(),
        )
        return

    if q.data.startswith("style:"):
        chosen_style = q.data.split(":", 1)[1]
        state = store.get(cid)
        if not state:
            state = CaseState(
                cid,
                document_type="dava_plaint",
                style_mode=chosen_style,
                style_selected=True,
                type_selected=chosen_style == "single_page_mode",
                single_page_mode=chosen_style == "single_page_mode",
                target_page_number=1 if chosen_style == "single_page_mode" else 0,
            )
        else:
            state.style_mode = chosen_style
            state.style_selected = True
            state.single_page_mode = chosen_style == "single_page_mode"
            if chosen_style == "single_page_mode" and not state.target_page_number:
                state.target_page_number = 1
        store.save(uid, state)
        style_label = STYLE_NAMES.get(chosen_style, chosen_style)
        plan_info = access.get_plan_status(uid)

        if chosen_style == "single_page_mode":
            await _safe_edit_text(
                q,
                "🏛️ *VAKIL DRAFTING AI — SINGLE-PAGE RECREATION STUDIO*\n"
                f"{DIVIDER}\n"
                f"✅ *चयनित मोड:* {style_label}\n"
                f"🏷️ *सदस्यता:* {plan_info['plan_label']}\n"
                f"{DIVIDER}\n"
                "👇 *किसी पुराने केस का केवल एक पन्ना (Single Page) सुधार के साथ दोबारा बनाने हेतु:*\n"
                "• नीचे से वह **पृष्ठ संख्या (Page 1..6)** चुनें जिसे आप री-क्रिएट करना चाहते हैं, या\n"
                "• सीधे उस पन्ने की **Photo 📸, PDF 📄, या Text ✍️** भेजें और बताएं कि उसमें क्या सुधार करना है _(जैसे: `पेज 3 के पैरा 9 में वर्ष 2022 की जगह 2024 कर दो`)_:",
                parse_mode="Markdown",
                reply_markup=single_page_selector_menu(state),
            )
            return

        if (state.facts or state.facts.get("raw_source_text")) and not missing_fields(state.facts, state.document_type, style_mode=chosen_style):
            await _safe_edit_text(
                q,
                f"✅ *चयनित ड्राफ्टिंग शैली:* {style_label}\n\n{summary_text(state)}\n"
                "👉 अब आप सीधे *⚡ ड्राफ्ट तैयार करें (Generate)* दबा सकते हैं:",
                parse_mode="Markdown",
                reply_markup=case_actions(state),
            )
            return

        if chosen_style == "as_is_mode":
            await _safe_edit_text(
                q,
                "🏛️ *VAKIL DRAFTING AI — AS-IT-IS & HANDWRITTEN LEGAL STUDIO*\n"
                f"{DIVIDER}\n"
                f"✅ *चयनित मोड:* {style_label}\n"
                f"🏷️ *सदस्यता:* {plan_info['plan_label']}\n"
                f"{DIVIDER}\n"
                "👇 कृपया नीचे से **दस्तावेज़ का प्रकार (Document Type)** चुनें, या सीधे अपने **A4 कागज़ / हस्तलिखित ड्राफ्ट (Handwritten Draft)** की **Photo 📸, PDF 📄, Word (.docx), Text ✍️, या Voice Note 🎙️** भेजें:",
                parse_mode="Markdown",
                reply_markup=dashboard(),
            )
            return

        await _safe_edit_text(
            q,
            "🏛️ *VAKIL DRAFTING AI — CASE WORKSPACE*\n"
            f"{DIVIDER}\n"
            f"✅ *चयनित शैली:* {style_label}\n"
            f"🏷️ *सदस्यता:* {plan_info['plan_label']}\n"
            f"{DIVIDER}\n"
            "👇 कृपया नीचे से **दस्तावेज़ का प्रकार (Document Type)** चुनें, या सीधे अपने केस का विवरण **Voice Note 🎙️, Text ✍️, या दस्तावेज़ की Photo/PDF 📸** में भेजें:",
            parse_mode="Markdown",
            reply_markup=dashboard(),
        )
        return

    if q.data == "single_page_studio":
        state = store.get(cid)
        await _safe_edit_text(
            q,
            "🏛️ *VAKIL DRAFTING AI — SINGLE-PAGE RECREATION & EXPORT STUDIO*\n"
            f"{DIVIDER}\n"
            "📃 **एकल पन्ना पुनर्निर्माण एवं एक्सपोर्ट स्टूडियो:**\n\n"
            "• यदि आप वर्तमान तैयार ड्राफ्ट में से **केवल एक विशेष पन्ना (PDF Page 1, 2, 3...)** अलग से प्रिंट करने के लिए निकालना चाहते हैं, तो नीचे **📄 PDF पृष्ठ निकालें** बटन दबाएं।\n"
            "• यदि आप किसी पन्ने को सुधार के साथ री-क्रिएट करना चाहते हैं, तो **📃 पृष्ठ री-क्रिएट करें** चुनें:",
            parse_mode="Markdown",
            reply_markup=single_page_selector_menu(state),
        )
        return

    if q.data.startswith("export_pdf_page:"):
        pg_no = int(q.data.split(":", 1)[1])
        state = store.get(cid)
        await export_single_pdf_page(update, uid, state, pg_no)
        return

    if q.data.startswith("select_recreate_page:"):
        pg_no = int(q.data.split(":", 1)[1])
        state = store.get(cid)
        if not state:
            state = CaseState(
                cid,
                document_type="dava_plaint",
                style_mode="single_page_mode",
                style_selected=True,
                type_selected=True,
                single_page_mode=True,
                target_page_number=pg_no,
            )
        else:
            state.style_mode = "single_page_mode"
            state.style_selected = True
            state.single_page_mode = True
            state.target_page_number = pg_no
        state.facts["target_page_number"] = pg_no
        store.save(uid, state)
        if state.facts.get("raw_source_pages") and len(state.facts["raw_source_pages"]) >= pg_no:
            await _safe_edit_text(
                q,
                f"✅ **पृष्ठ {pg_no} री-क्रिएशन के लिए चयनित है!**\n"
                f"{DIVIDER}\n"
                f"• यदि आपको पृष्ठ {pg_no} में कोई नाम, वर्ष या शब्द बदलना है, तो उसे अभी लिखकर भेजें _(जैसे: `पैरा 5 में 2023 की जगह 2024 कर दो`)_,\n"
                f"• या सीधे नीचे **⚡ केवल पृष्ठ {pg_no} तैयार करें** बटन दबाएँ:",
                parse_mode="Markdown",
                reply_markup=case_actions(state),
            )
            return
        await _safe_edit_text(
            q,
            f"✅ **पृष्ठ {pg_no} री-क्रिएशन मोड सक्रिय है!**\n"
            f"{DIVIDER}\n"
            f"📥 अब कृपया **पृष्ठ {pg_no}** की **Photo 📸, PDF 📄, या Text ✍️** भेजें और साथ में लिखें कि उसमें क्या सुधार करना है।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ वापस जाएं", callback_data="review")]]),
        )
        return

    if q.data == "view_sections":
        state = store.get(cid)
        if not state or not state.draft:
            await q.edit_message_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है।")
            return
        map_text = format_draft_section_map(state.draft)
        await _safe_edit_text(
            q,
            f"🏛️ *VAKIL DRAFTING AI — DOCUMENT SECTION MAP (v{state.draft_version})*\n"
            f"{DIVIDER}\n"
            f"{map_text}\n"
            f"{DIVIDER}\n"
            "💡 _किसी विशेष पैरा या हेडिंग में बदलाव हेतु लिखें: `सिर्फ पैरा 3 में ... कर दो` या `शीर्षक में ... बदल दो`।_",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
        return

    if q.data == "audit_draft":
        state = store.get(cid)
        if not state or not state.draft:
            await q.edit_message_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है।")
            return
        findings = audit_draft_consistency(state.draft, state.document_type)
        await _safe_edit_text(
            q,
            f"🏛️ *VAKIL DRAFTING AI — PRE-FILING SMART AUDIT (v{state.draft_version})*\n"
            f"{DIVIDER}\n"
            + "\n\n".join(findings)
            + f"\n{DIVIDER}\n"
            "💡 _किसी भी रिक्त स्थान या त्रुटि को ठीक करने के लिए सीधे लिखकर या Voice Note 🎙️ से निर्देश भेजें।_",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
        return

    if q.data == "undo_draft":
        state = store.get(cid)
        if not state or not state.draft:
            await q.edit_message_text("ℹ️ अभी कोई तैयार ड्राफ्ट उपलब्ध नहीं है।")
            return
        if not state.undo_draft():
            await q.edit_message_text("ℹ️ यह ड्राफ्ट का प्रथम संस्करण (`v1`) है; इससे पूर्व कोई संस्करण उपलब्ध नहीं है।", reply_markup=case_actions(state))
            return
        store.save(uid, state)
        await download_current_draft(update, uid, state, edit_message=True)
        return

    if q.data.startswith("toggle_scope:"):
        target_scope = q.data.split(":", 1)[1]
        state = store.get(cid)
        if not state or not state.draft or not state.pending_edit:
            await q.edit_message_text("⚠️ यह संशोधन अब उपलब्ध नहीं है।")
            return
        cur_edit = state.pending_edit.get("edit") or {}
        try:
            new_edit = convert_edit_scope(state.draft, cur_edit, target_scope)
            candidate, preview = await asyncio.to_thread(editor.apply, state.draft, new_edit)
            state.pending_edit["edit"] = new_edit
            state.pending_edit["candidate"] = candidate
            store.save(uid, state)
            scope_note = (
                "🌐 *दायरा अपडेट किया गया: पूरे ड्राफ्ट में सभी जगह (Global Scope)*"
                if target_scope == "global"
                else "🎯 *दायरा अपडेट किया गया: केवल लक्षित पैरा/हेडिंग में (Targeted Scope)*"
            )
            await _safe_edit_text(
                q,
                "🏛️ *VAKIL DRAFTING AI — CHANGE PREVIEW (UPDATED SCOPE)*\n"
                f"{DIVIDER}\n"
                f"{scope_note}\n\n"
                f"{preview}\n"
                f"{DIVIDER}\n"
                "👇 *क्या आप इस संशोधन को ड्राफ्ट में लागू करना चाहते हैं?*",
                parse_mode="Markdown",
                reply_markup=edit_actions(state),
            )
        except Exception as exc:
            log.exception("toggle_scope failed")
            await q.edit_message_text(f"⚠️ दायरा बदलने में त्रुटि: {str(exc)[:300]}", reply_markup=edit_actions(state))
        return

    if q.data == "switch_style":
        state = store.get(cid)
        cur = state.style_mode if state else "template_mode"
        await _safe_edit_text(
            q,
            "🏛️ *VAKIL DRAFTING AI — SWITCH ENGINE STYLE*\n"
            f"{DIVIDER}\n"
            "🔄 इसी केस को दूसरी न्यायिक शैली में पुनः तैयार करने के लिए नीचे से विकल्प चुनें:",
            parse_mode="Markdown",
            reply_markup=switch_style_menu(cur),
        )
        return

    if q.data.startswith("regen_style:"):
        chosen_style = q.data.split(":", 1)[1]
        state = store.get(cid)
        if not state or not (state.facts or state.facts.get("raw_source_text")):
            await q.edit_message_text("कृपया पहले केस का विवरण भेजें।", reply_markup=style_selection_menu())
            return
        state.style_mode = chosen_style
        state.style_selected = True
        state.single_page_mode = chosen_style == "single_page_mode"
        store.save(uid, state)
        await generate_for(update, uid, state, edit=True)
        return

    if q.data == "generate_as_is":
        state = store.get(cid)
        if not state or not (state.facts or state.facts.get("raw_source_text")):
            await q.edit_message_text("कृपया पहले दस्तावेज़ की Photo, PDF या Text भेजें।", reply_markup=style_selection_menu())
            return
        state.style_mode = "as_is_mode"
        state.style_selected = True
        state.single_page_mode = False
        store.save(uid, state)
        await generate_for(update, uid, state, edit=True)
        return

    if q.data == "download_current":
        state = store.get(cid)
        await download_current_draft(update, uid, state, edit_message=True)
        return

    if q.data == "add_point_mode":
        state = store.get(cid)
        if not state or not state.draft:
            await q.edit_message_text("कृपया पहले एक ड्राफ्ट तैयार करें।")
            return
        state.edit_mode = True
        state.pending_point_action = "add"
        store.save(uid, state)
        await _safe_edit_text(
            q,
            "🏛️ *VAKIL DRAFTING AI — ADD PLEADING POINT*\n"
            f"{DIVIDER}\n"
            "➕ इस ड्राफ्ट में आप जो भी अतिरिक्त तथ्य, धारा या प्रार्थना बिंदु जोड़ना चाहते हैं, उसे सीधे लिखकर या **Voice Note 🎙️** बोलकर भेजें।\n\n"
            "*उदाहरण:*\n"
            "• `यह बिंदु जोड़ दो कि प्रतिवादी ने पंचों के समक्ष समझौता स्वीकार किया था।`\n"
            "• `प्रार्थना में मौके के निरीक्षण हेतु अमीन कमीशन नियुक्त करने का बिंदु जोड़ दो।`",
            parse_mode="Markdown",
        )
        return

    if q.data == "remove_point_mode":
        state = store.get(cid)
        if not state or not state.draft:
            await q.edit_message_text("कृपया पहले एक ड्राफ्ट तैयार करें।")
            return
        state.edit_mode = True
        state.pending_point_action = "remove"
        store.save(uid, state)
        pleadings = state.draft.get("pleadings") or []
        preview_lines = []
        for idx, p in enumerate(pleadings[:12], 1):
            short = str(p).strip()
            if len(short) > 70:
                short = short[:70] + "…"
            preview_lines.append(f"`{idx}.` {short}")
        para_block = "\n".join(preview_lines) if preview_lines else "—"
        await _safe_edit_text(
            q,
            "🏛️ *VAKIL DRAFTING AI — REMOVE PLEADING POINT*\n"
            f"{DIVIDER}\n"
            f"📋 *वर्तमान पैराग्राफ सूची:*\n{para_block}\n"
            f"{DIVIDER}\n"
            "🗑️ जो पैराग्राफ या बिंदु हटाना हो, उसकी संख्या या विवरण लिखकर या **Voice Note 🎙️** से भेजें _(जैसे: `पैराग्राफ 3 हटा दो`)_।",
            parse_mode="Markdown",
        )
        return

    if q.data == "case_documents":
        state = store.get(cid)
        if not state or not state.documents:
            await q.edit_message_text("इस केस में अभी कोई तैयार दस्तावेज़ नहीं है।")
            return
        await _safe_edit_text(
            q,
            f"🏛️ *VAKIL DRAFTING AI — CASE DOCUMENTS*\n{DIVIDER}\n🗂️ इस केस में तैयार किए गए सभी दस्तावेज़:",
            parse_mode="Markdown",
            reply_markup=existing_documents_menu(state),
        )
        return

    if q.data.startswith("doc:"):
        state = store.get(cid)
        did = q.data.split(":", 1)[1]
        item = next((x for x in (state.documents if state else []) if x.get("document_id") == did), None)
        if not state or not item:
            await q.edit_message_text("⚠️ यह दस्तावेज़ अब उपलब्ध नहीं है।")
            return
        state.document_type = str(item.get("document_type"))
        state.type_selected = True
        state.style_mode = str(item.get("style_mode", state.style_mode))
        state.selected_template_name = str(item.get("selected_template_name", ""))
        state.active_document_id = did
        state.draft = item.get("draft") or {}
        state.draft_version = int(item.get("version", 1) or 1)
        state.draft_versions = [{"version": state.draft_version, "draft": state.draft}]
        state.edit_mode = True
        state.pending_edit = {}
        state.pending_point_action = ""
        state.status = "drafted"
        store.save(uid, state)
        await _safe_edit_text(
            q,
            f"🏛️ *VAKIL DRAFTING AI — ACTIVE DOCUMENT*\n"
            f"{DIVIDER}\n"
            f"📄 *चयनित दस्तावेज़:* {DISPLAY_NAMES[state.document_type]} (v{state.draft_version})\n"
            f"🎨 *शैली:* {STYLE_NAMES.get(state.style_mode, state.style_mode)}\n\n"
            "अब आप इसे डाउनलोड कर सकते हैं, इसमें संशोधन कर सकते हैं या इसी केस में अगला दस्तावेज़ जोड़ सकते हैं:",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
        return

    if q.data == "add_document":
        state = store.get(cid)
        await _safe_edit_text(
            q,
            "🏛️ *VAKIL DRAFTING AI — MULTI-DOCUMENT SUITE*\n"
            f"{DIVIDER}\n"
            "📑 *इसी केस के विवरण से अगला सह-दस्तावेज़ (Jawab Dava, Affidavit, 6C Application आदि) चुनें:*",
            parse_mode="Markdown",
            reply_markup=document_menu(state),
        )
        return

    if q.data.startswith("addtype:"):
        t = q.data.split(":", 1)[1]
        state = store.get(cid)
        if not state or not state.facts:
            await q.edit_message_text("कृपया पहले एक केस तैयार करें।", reply_markup=style_selection_menu())
            return
        if state.document_type == t and state.draft:
            await q.edit_message_text("यह दस्तावेज़ अभी सक्रिय है। कृपया कोई दूसरा प्रकार चुनें।", reply_markup=document_menu(state))
            return
        if state.style_mode in {"as_is_mode", "single_page_mode"}:
            state.style_mode = "template_mode"
            state.single_page_mode = False
        state.begin_new_document(t)
        try:
            await asyncio.to_thread(orchestrator.refresh_intake, state)
            store.save(uid, state)
        except Exception:
            log.exception("additional document callback reconciliation failed")
        if missing_fields(state.facts, t, style_mode=state.style_mode):
            await _safe_edit_text(q, f"📑 *{DISPLAY_NAMES[t]}*\n\n{questions_text(state)}", parse_mode="Markdown", reply_markup=case_actions(state))
            return
        await _safe_edit_text(q, f"⚙️ *{DISPLAY_NAMES[t]}* आपके केस के तथ्यों से तैयार किया जा रहा है…", parse_mode="Markdown")
        await generate_for(update, uid, state, edit=True)
        return

    if q.data.startswith("type:"):
        t = q.data.split(":", 1)[1]
        existing = store.get(cid)
        chosen_style = existing.style_mode if existing else "template_mode"
        style_sel = existing.style_selected if existing else True
        state = CaseState(
            cid,
            document_type=t,
            style_mode=chosen_style,
            style_selected=style_sel,
            type_selected=True,
            single_page_mode=chosen_style == "single_page_mode",
            target_page_number=existing.target_page_number if existing else 0,
        )
        store.save(uid, state)
        await _safe_edit_text(
            q,
            f"🏛️ *VAKIL DRAFTING AI — CASE INTAKE*\n"
            f"{DIVIDER}\n"
            f"📄 *दस्तावेज़:* {DISPLAY_NAMES[t]}\n"
            f"📊 *पूर्णता (Readiness):* `{progress(state)}`\n\n"
            f"{questions_text(state)}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 हूबहू / हस्तलिखित मोड (As-It-Is Mode)", callback_data="style:as_is_mode")] if chosen_style != "as_is_mode" else [InlineKeyboardButton("🔄 ड्राफ्टिंग शैली बदलें (Change Style)", callback_data="choose_style")],
                [InlineKeyboardButton("📃 एकल पन्ना री-क्रिएट मोड (Single Page)", callback_data="style:single_page_mode")],
                [InlineKeyboardButton("🔄 ड्राफ्टिंग शैली बदलें (Change Style)", callback_data="choose_style")] if chosen_style != "as_is_mode" else [InlineKeyboardButton("❌ सेशन निरस्त करें (Cancel)", callback_data="cancel")],
                *([[InlineKeyboardButton("❌ सेशन निरस्त करें (Cancel)", callback_data="cancel")]] if chosen_style != "as_is_mode" else []),
            ]),
        )
        return

    state = store.get(cid)
    if q.data == "cancel":
        store.delete(cid)
        await _safe_edit_text(
            q,
            "🏛️ *VAKIL DRAFTING AI*\n"
            f"{DIVIDER}\n"
            "✅ वर्तमान ड्राफ्टिंग सेशन समाप्त कर दिया गया है। `/newcase` से नया केस शुरू करें।",
            parse_mode="Markdown",
        )
        return
    if not state:
        await _safe_edit_text(q, "ℹ️ कोई सक्रिय ड्राफ्ट नहीं है। `/newcase` से शुरू करें।", parse_mode="Markdown")
        return
    if q.data == "review":
        await _safe_edit_text(q, summary_text(state), parse_mode="Markdown", reply_markup=case_actions(state))
        return
    if q.data == "edit_mode":
        state.edit_mode = True
        state.pending_point_action = ""
        state.pending_edit = {}
        store.save(uid, state)
        await _safe_edit_text(
            q,
            "🏛️ *VAKIL DRAFTING AI — SCOPE-AWARE SMART EDIT STUDIO*\n"
            f"{DIVIDER}\n"
            "✏️ अब अपना संशोधन लिखकर या **Voice Note 🎙️** से भेजें।\n\n"
            "🎯 *केवल एक पैरा या हेडिंग में बदलाव (Targeted):*\n"
            "• `सिर्फ पैरा 3 में 15.06.2026 के स्थान पर 20.06.2026 कर दो।`\n"
            "• `शीर्षक में ... बदल दो।`\n\n"
            "🌐 *पूरे ड्राफ्ट में हर जगह बदलाव (Global):*\n"
            "• `पूरे ड्राफ्ट में गाटा संख्या 412 के स्थान पर 415 कर दो।`\n\n"
            "_हर निर्देश पर पहले Preview और Scope Toggle बटन दिखाया जाएगा, जिसे आप Apply या Discard कर सकेंगे।_",
            parse_mode="Markdown",
        )
        return
    if q.data == "edit_apply":
        await apply_edit(update, context)
        return
    if q.data == "edit_discard":
        state.pending_edit = {}
        state.pending_point_action = ""
        store.save(uid, state)
        await q.edit_message_text("↩️ प्रस्तावित संशोधन निरस्त कर दिया गया है। आपका मूल ड्राफ्ट सुरक्षित है।", reply_markup=case_actions(state))
        return
    if q.data == "generate":
        if state.style_mode not in {"as_is_mode", "single_page_mode"} and not getattr(state, "single_page_mode", False):
            try:
                await _safe_edit_text(q, "⚙️ *तथ्यों का सत्यापन एवं उपयुक्त न्यायिक प्रारूप का चयन किया जा रहा है…*", parse_mode="Markdown")
                await asyncio.to_thread(orchestrator.refresh_intake, state)
                store.save(uid, state)
            except Exception:
                log.exception("pre-generation intake reconciliation failed")
        if missing_fields(state.facts, state.document_type, style_mode=state.style_mode):
            await _safe_edit_text(q, questions_text(state), parse_mode="Markdown", reply_markup=case_actions(state))
            return
        await generate_for(update, uid, state, edit=True)


def build_application():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    app = Application.builder().token(token).concurrent_updates(True).pool_timeout(30.0).connection_pool_size(32).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newcase", newcase))
    app.add_handler(CommandHandler("asis", asis_command))
    app.add_handler(CommandHandler("retype", asis_command))
    app.add_handler(CommandHandler("page", page_command))
    app.add_handler(CommandHandler("singlepage", page_command))
    app.add_handler(CommandHandler("sections", sections_command))
    app.add_handler(CommandHandler("audit", audit_command))
    app.add_handler(CommandHandler("undo", undo_command))
    app.add_handler(CommandHandler("style", style_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("download", download_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("corpus", corpus_info))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("adddocument", add_document_command))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("myplan", myplan_command))
    app.add_handler(CommandHandler("quota", myplan_command))
    app.add_handler(CommandHandler("trial", trial_command))
    app.add_handler(CommandHandler("authorize_trial", trial_command))
    app.add_handler(CommandHandler("paid", paid_command))
    app.add_handler(CommandHandler("authorize_paid", paid_command))
    app.add_handler(CommandHandler("authorize", authorize))
    app.add_handler(CommandHandler("unauthorize", unauthorize))
    app.add_handler(CommandHandler("authorized", authorized))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_message))
    app.add_handler(MessageHandler(filters.PHOTO, image_document_message))
    app.add_handler(MessageHandler(filters.Document.ALL, image_document_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return app


def main():
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()