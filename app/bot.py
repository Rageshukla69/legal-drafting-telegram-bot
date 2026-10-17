"""Professional Telegram UI for Indian legal drafts — supports explicit Style Selection
(500+ Purchased Legal Templates vs Advocate B.D. Shukla's 306 Original Files vs Hybrid),
auto-template matching, adding/removing points, multi-document cases (Affidavit, Jawab Dava, etc.),
and voice/text/photo workflows.
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
from app.drafting_engine.renderers.legal_document_renderer import render_both
from app.drafting_engine.azure_speech import transcribe_voice
from app.drafting_engine.gemini_client import GeminiError
from app.drafting_engine.draft_editor import DraftEditor
from app.drafting_engine.document_intelligence import analyze_document_bytes, DocumentIntelligenceError
from app.drafting_engine.krutidev_converter import decode_krutidev_hybrid

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("legal-bot")

store = CaseStore()
access = AccessController(store, allow_unset=True)
orchestrator = MultiDraftOrchestrator()
editor = DraftEditor()

TYPE_ORDER = [
    "dava_plaint", "written_statement", "application",
    "evidence_pw_affidavit", "affidavit", "legal_notice", "other_civil"
]
TYPE_ICONS = {
    "dava_plaint": "📜", "written_statement": "📝", "application": "📄",
    "evidence_pw_affidavit": "⚖️", "affidavit": "📋", "legal_notice": "📬", "other_civil": "📑"
}


def case_id_for(user_id: int) -> str:
    return f"tg-{user_id}-current"


def get_state(uid: int):
    return store.get(case_id_for(uid))


def style_selection_menu():
    """Primary menu asking the user whether to use the 500+ Purchased Templates or Dad's 306 Files."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 500+ Purchased Legal Templates", callback_data="style:template_mode")],
        [InlineKeyboardButton("🏛️ Dad's 306 Original Files Style", callback_data="style:dad_style")],
        [InlineKeyboardButton("⚡ Hybrid Mix (Template + Dad's Style)", callback_data="style:hybrid_mode")],
    ])


def switch_style_menu(current_style: str = "template_mode"):
    rows = []
    for mode_key, label in STYLE_NAMES.items():
        prefix = "✅ " if mode_key == current_style else ""
        rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"regen_style:{mode_key}")])
    rows.append([InlineKeyboardButton("↩️ Back", callback_data="review")])
    return InlineKeyboardMarkup(rows)


def dashboard():
    rows = []
    for i in range(0, len(TYPE_ORDER), 2):
        row = []
        for t in TYPE_ORDER[i:i + 2]:
            row.append(InlineKeyboardButton(f"{TYPE_ICONS[t]} {DISPLAY_NAMES[t]}", callback_data=f"type:{t}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔄 Change Style (Dad's ↔ 500+ Templates)", callback_data="choose_style")])
    return InlineKeyboardMarkup(rows)


def case_actions(state):
    rows = [
        [
            InlineKeyboardButton("✨ Generate Draft", callback_data="generate"),
            InlineKeyboardButton("👁 Review Facts", callback_data="review"),
        ]
    ]
    if state.draft:
        rows.append([
            InlineKeyboardButton("➕ Add Point", callback_data="add_point_mode"),
            InlineKeyboardButton("🗑️ Remove Point", callback_data="remove_point_mode"),
        ])
        rows.append([
            InlineKeyboardButton("✏️ Edit Any Detail", callback_data="edit_mode"),
            InlineKeyboardButton("🔄 Switch Style", callback_data="switch_style"),
        ])
        rows.append([
            InlineKeyboardButton("➕ Add Another Document (Affidavit / Jawab Dava)", callback_data="add_document")
        ])
    else:
        rows.append([
            InlineKeyboardButton("🔄 Switch Style (Dad's ↔ 500+ Templates)", callback_data="choose_style")
        ])
        if state.documents:
            rows.append([
                InlineKeyboardButton("➕ Add Another Document", callback_data="add_document")
            ])

    if len(state.documents) > 1:
        rows.append([InlineKeyboardButton(f"📚 Case Documents ({len(state.documents)})", callback_data="case_documents")])
    rows.append([InlineKeyboardButton("🗑 Cancel Session", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def document_menu(state=None):
    rows = []
    for i in range(0, len(TYPE_ORDER), 2):
        rows.append([
            InlineKeyboardButton(f"{TYPE_ICONS[t]} {DISPLAY_NAMES[t]}", callback_data=f"addtype:{t}")
            for t in TYPE_ORDER[i:i + 2]
        ])
    rows.append([InlineKeyboardButton("🔄 Change Style for Next Document", callback_data="choose_style")])
    rows.append([InlineKeyboardButton("↩️ Keep Current Document", callback_data="review")])
    return InlineKeyboardMarkup(rows)


def existing_documents_menu(state):
    rows = []
    for item in state.documents[-20:][::-1]:
        did = str(item.get("document_id", ""))
        typ = str(item.get("document_type", ""))
        if not did or typ not in DISPLAY_NAMES:
            continue
        marker = "✅ " if did == state.active_document_id else ""
        rows.append([InlineKeyboardButton(f"{marker}{DISPLAY_NAMES[typ]}", callback_data=f"doc:{did}")])
    rows.append([InlineKeyboardButton("➕ Add Another Document", callback_data="add_document")])
    return InlineKeyboardMarkup(rows)


_TYPE_KEYWORDS = {
    "written_statement": ("written statement", "लिखित कथन", "जवाब दावा", "जवाब-दावा", "प्रतिवादपत्र", "प्रतिवाद पत्र", "लिखित बयान", "ws", "jawab dava"),
    "application": ("application", "प्रार्थना पत्र", "प्रार्थना-पत्र", "प्रार्थनापत्र", "आवेदन", "कमीशन", "6ग", "संशोधन", "39 नियम 1", "7 नियम 11", "स्टे प्रार्थना पत्र"),
    "evidence_pw_affidavit": ("evidence affidavit", "pw affidavit", "साक्ष्य शपथपत्र", "साक्ष्य का शपथपत्र", "बयान चीफ", "पी डब्लू", "डी डब्लू", "pw-1", "dw-1", "pw1"),
    "affidavit": ("affidavit", "शपथपत्र", "हलफनामा", "शपथ पत्र", "shapath patra"),
    "legal_notice": ("legal notice", "कानूनी नोटिस", "विधिक नोटिस", "रजिस्टर्ड नोटिस", "138 ni", "धारा 138"),
    "other_civil": ("other civil", "अन्य सिविल", "सिविल ड्राफ्ट", "कैवियट", "केवियट", "caveat", "तलाशपता", "इकरारनामा"),
    "dava_plaint": ("dava", "दावा", "वाद पत्र", "वाद-पत्र", "वादपत्र", "plaint", "plaintiff suit", "स्थायी निषेधाज्ञा", "बंटवारा", "विशिष्ट अनुपालन"),
}


def infer_document_type(text: str, default: str = "dava_plaint") -> str:
    """Infer document type from natural-language Hindi/English dictation or OCR text."""
    low = re.sub(r"\s+", " ", str(text or "").strip().casefold())
    if not low:
        return default
    for typ, markers in _TYPE_KEYWORDS.items():
        if any(m in low for m in markers):
            return typ
    return default


def detect_additional_document_type(text: str):
    """Detect an explicit request to create another document while a draft exists."""
    low = re.sub(r"\s+", " ", str(text or "").strip().casefold())
    if not low:
        return None
    create_markers = (
        "बना", "बनाओ", "बना दो", "तैयार", "तैयार कर", "ड्राफ्ट", "create",
        "generate", "draft", "make", "prepare", "चाहिए"
    )
    if not any(x in low for x in create_markers):
        return None
    for typ, markers in _TYPE_KEYWORDS.items():
        if any(m in low for m in markers):
            return typ
    return None


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
        "replace", "change", "edit", "delete", "remove", "insert", "add", "point",
        "correct", "update", "modify", "amend", "next page", "page 2",
    )
    return any(m in low for m in edit_markers)


def edit_actions():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Apply Change", callback_data="edit_apply"), InlineKeyboardButton("❌ Discard", callback_data="edit_discard")]
    ])


def progress(state):
    missing = missing_fields(state.facts, state.document_type)
    total = len(missing_fields({}, state.document_type))
    done = max(0, total - len(missing))
    pct = int(done / total * 100) if total else 100
    filled = "█" * max(1, pct // 10)
    empty = "░" * (10 - len(filled))
    return f"{filled}{empty} {pct}%"


def questions_text(state):
    qs = next_questions(state.facts, state.document_type)
    style_label = STYLE_NAMES.get(state.style_mode, state.style_mode)
    if not qs:
        return f"🎨 *Selected Style:* {style_label}\n\nसारी आवश्यक जानकारी मिल गई है। नीचे *✨ Generate Draft* दबाएँ।"
    return (
        f"🎨 *Selected Style:* {style_label}\n\n"
        "कृपया अपने केस का विवरण भेजें (आप एक साथ बोलकर, लिखकर या फोटो भेजकर बता सकते हैं):\n\n"
        + "\n".join(f"• {label}" for _, label in qs)
        + "\n• कचहरी / Court Complex व अधिवक्ता का नाम (यदि अलग हो)"
        + "\n• कोई अतिरिक्त पॉइंट जो जोड़ना या हटाना हो"
        + "\n\nबोट आपके केस के अनुसार सबसे उपयुक्त टेम्पलेट चुनकर उसमें ये सभी विवरण भर देगा।"
    )


def summary_text(state):
    facts = state.facts
    style_label = STYLE_NAMES.get(state.style_mode, state.style_mode)

    def val(k):
        v = facts.get(k, "—")
        return "\n".join(f"• {x}" for x in v) if isinstance(v, list) else str(v)

    extra = ""
    if state.selected_template_name:
        extra += f"*Selected Template:* `{state.selected_template_name}`\n"
    if facts.get("advocate_name") or facts.get("court_complex"):
        extra += f"*अधिवक्ता / कचहरी:* {facts.get('advocate_name', 'वी०डी० शुक्ला एडवोकेट')} ({facts.get('court_complex', 'सिविल कोर्ट')})\n"

    return (
        f"⚖️ *{DISPLAY_NAMES[state.document_type]}*\n"
        f"🎨 *Style:* {style_label}\n"
        f"{extra}\n"
        f"*Progress:* {progress(state)}\n\n"
        f"*न्यायालय:* {val('court_name')}\n"
        f"*पक्षकार:* {val('parties') or val('plaintiffs')}\n"
        f"*मुख्य तथ्य / विवाद:* {val('facts')}\n"
        f"*राहत/मांग:* {val('reliefs') if facts.get('reliefs') else val('demands')}\n"
    )


async def deny(update):
    if update.message:
        await update.message.reply_text(
            "🔒 *Private Legal Drafting Bot*\n\n"
            "आप इस bot का उपयोग करने के लिए authorized नहीं हैं।\n"
            "अपना Telegram User ID देखने के लिए /myid भेजें।",
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
    await update.message.reply_text(f"🆔 आपका Telegram User ID है: `{update.effective_user.id}`", parse_mode="Markdown")


async def corpus_info(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return

    idx_path = Path(__file__).resolve().parent.parent / "corpus_index.json"
    total_docs = 0
    dad_count = 0
    tpl_count = 0
    if idx_path.exists():
        try:
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            total_docs = len(data.get("documents", []))
            counts = data.get("source_counts", {})
            dad_count = counts.get("dad_corpus", 0)
            tpl_count = counts.get("professional_template", 0)
        except Exception:
            pass

    ai_model = os.getenv("AZURE_AI_MODEL", "gpt-6.0-luna")
    fallback_model = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")

    msg = (
        "📚 *LEGAL CORPUS & STYLE MODES*\n\n"
        f"🤖 *Primary Drafting Engine:* `{ai_model}`\n"
        f"🛡️ *Fallback Engine:* `{fallback_model}`\n\n"
        f"📊 *Total Indexed Documents:* {total_docs}\n"
        f"  • *📜 Purchased Legal Templates:* {tpl_count}\n"
        f"  • *🏛️ Dad's 306 Original Files:* {dad_count}\n\n"
        "🎨 *Available Style Modes (/newcase or /style):*\n"
        "1. *📜 500+ Purchased Legal Templates:* Picks the best template from your purchased collection and fills in your case details.\n"
        "2. *🏛️ Dad's 306 Original Files Style:* Drafts in Advocate B.D. Shukla's signature court language.\n"
        "3. *⚡ Hybrid Mix:* Combines purchased template structure with Dad's legal phrasing."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def help_command(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    msg = (
        "⚖️ *LEGAL DRAFTING BOT GUIDE*\n\n"
        "*Commands:*\n"
        "• /newcase — नया केस शुरू करें (पहले Style चुनें: 500+ Templates या Dad's 306 Files)\n"
        "• /style — किसी भी समय Drafting Style बदलें\n"
        "• /adddocument — इसी केस में Affidavit, Jawab Dava, Application आदि बनाएं\n"
        "• /edit — ड्राफ्ट में कोई भी पॉइंट जोड़ें, हटाएं या बदलें\n"
        "• /summary — वर्तमान केस का विवरण देखें\n"
        "• /corpus — टेम्पलेट और कॉर्पस की संख्या देखें\n"
        "• /cancel — वर्तमान सेशन बंद करें\n\n"
        "*How to Use:*\n"
        "1. `/newcase` दबाकर चुनें कि आपको **500+ खरीदे गए टेम्पलेट्स** का उपयोग करना है या **पिताजी की 306 फाइलों की स्टाइल** का।\n"
        "2. केस का विवरण (विवाद, पक्षकार, कोर्ट परिसर, पिताजी का नाम) बोलकर (Voice Note), लिखकर या फोटो भेजकर बताएं।\n"
        "3. बोट सबसे उपयुक्त टेम्पलेट चुनकर उसमें आपके केस का विवरण भर देगा और DOCX + PDF देगा।\n"
        "4. उसके बाद आप **➕ Add Point** या **🗑️ Remove Point** से कोई भी बिंदु जोड़ या हटा सकते हैं!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def authorize(update, context):
    if not update.message or not update.effective_user:
        return
    if not access.is_owner(update.effective_user.id):
        await deny(update)
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/authorize TELEGRAM_USER_ID`", parse_mode="Markdown")
        return
    target = context.args[0]
    if access.is_owner(target):
        await update.message.reply_text("👑 Owner पहले से permanently authorized है।")
        return
    access.authorize(target, update.effective_user.id)
    await update.message.reply_text(f"✅ User `{target}` को authorize कर दिया गया है।", parse_mode="Markdown")


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
        await update.message.reply_text("🛡️ Owner को unauthorize नहीं किया जा सकता।")
        return
    removed = access.unauthorize(target)
    await update.message.reply_text(
        f"{'✅ User removed from allowlist.' if removed else 'ℹ️ User allowlist में नहीं था.'}\nUser ID: `{target}`",
        parse_mode="Markdown",
    )


async def authorized(update, context):
    if not update.message or not update.effective_user:
        return
    if not access.is_owner(update.effective_user.id):
        await deny(update)
        return
    users = access.authorized_users()
    lines = [f"👑 Owner: `{access.owner_id}`", "", "*Authorized users:*"]
    lines.extend(f"• `{u['user_id']}`" for u in users)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


STYLE_PROMPT_TEXT = (
    "⚖️ *LEGAL DRAFTING WORKSPACE*\n\n"
    "नया केस शुरू करने के लिए सबसे पहले चुनें कि आप किस **Style / Template** का उपयोग करना चाहते हैं:\n\n"
    "1️⃣ *📜 500+ Purchased Legal Templates:* आपके खरीदे गए 500+ टेम्पलेट्स में से केस के लिए सबसे उपयुक्त टेम्पलेट चुनकर उसमें आपके केस के विवरण (पार्टी के नाम, कोर्ट परिसर, तथ्य, पिताजी का नाम) भर देगा।\n\n"
    "2️⃣ *🏛️ Dad's 306 Original Files Style:* आपके पिताजी (Adv. B.D. Shukla) की 306 मूल फाइलों की भाषा और प्रारूप में ड्राफ्ट तैयार करेगा।\n\n"
    "3️⃣ *⚡ Hybrid Mix (Both):* प्रोफेशनल टेम्पलेट के ढांचे और पिताजी की कानूनी भाषा का मिश्रण।"
)


async def start(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    state = CaseState(case_id_for(uid), document_type="dava_plaint", style_mode="template_mode", style_selected=False)
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
    state = CaseState(case_id_for(uid), document_type="dava_plaint", style_mode="template_mode", style_selected=False)
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
        "🎨 *Select Drafting Style:*\n\nआप किस स्टाइल में ड्राफ्ट तैयार करना चाहते हैं?",
        parse_mode="Markdown",
        reply_markup=style_selection_menu(),
    )


async def cancel(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    store.delete(case_id_for(update.effective_user.id))
    await update.message.reply_text("✅ Current draft session बंद कर दी गई है। /newcase से नया draft शुरू करें।")


async def summary(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state:
        await update.message.reply_text("कोई active draft नहीं है। /newcase दबाएँ।")
        return
    await update.message.reply_text(summary_text(state), parse_mode="Markdown", reply_markup=case_actions(state))


async def intake_text(update, context, text=None, *, suppress_questions=False, announce=True):
    if not update.effective_user:
        return
    if not access.is_authorized(update.effective_user.id):
        await deny(update)
        return
    uid = update.effective_user.id
    text = decode_krutidev_hybrid((text or update.message.text or "").strip())
    state = get_state(uid)
    if not state:
        inferred_type = infer_document_type(text, default="dava_plaint")
        state = CaseState(case_id_for(uid), document_type=inferred_type, style_mode="template_mode", style_selected=False)
        store.save(uid, state)
    elif not state.facts:
        # Auto-adjust document type on first substantive message if user didn't pick one explicitly
        inferred_type = infer_document_type(text, default=state.document_type)
        state.document_type = inferred_type

    state.record("user", text)
    if announce:
        await update.message.reply_text(f"⏳ {DISPLAY_NAMES[state.document_type]} के लिए आपके केस का विवरण समझा जा रहा है…")
    try:
        result = await asyncio.to_thread(orchestrator.extract_and_collect, state, text)
        store.save(uid, state)
    except Exception:
        log.exception("Intake failed")
        await update.message.reply_text("❌ जानकारी process नहीं हो सकी। कृपया दोबारा भेजें।")
        return
    if suppress_questions:
        return result

    # If the user sent case details directly before picking a style, ask them which style they want to use!
    if not state.style_selected:
        await update.message.reply_text(
            f"✅ आपके केस का विवरण नोट कर लिया गया है!\n\n{summary_text(state)}\n"
            "👉 *अब चुनें कि आप किस Style से ड्राफ्ट बनाना चाहते हैं:*",
            parse_mode="Markdown",
            reply_markup=style_selection_menu(),
        )
        return result

    if result["status"] == "needs_information":
        await update.message.reply_text(
            f"{questions_text(state)}\n\nProgress: {progress(state)}",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
        return result
    await update.message.reply_text(
        f"✅ आवश्यक जानकारी मिल गई।\n\n{summary_text(state)}",
        parse_mode="Markdown",
        reply_markup=case_actions(state),
    )
    return result


async def start_additional_document(update, context, document_type: str):
    uid = update.effective_user.id
    state = get_state(uid)
    if not state or not state.facts:
        await update.message.reply_text("पहले एक case तैयार करें, फिर उसमें दूसरा document जोड़ें।", reply_markup=style_selection_menu())
        return
    if state.document_type == document_type and state.draft:
        await update.message.reply_text("यह document type अभी active है। अलग document बनाने के लिए कोई दूसरा type चुनें।", reply_markup=document_menu(state))
        return
    state.begin_new_document(document_type)
    try:
        await asyncio.to_thread(orchestrator.refresh_intake, state)
        store.save(uid, state)
    except Exception:
        log.exception("additional document intake reconciliation failed")
    missing = missing_fields(state.facts, document_type)
    if missing:
        msg = (
            f"➕ *{DISPLAY_NAMES[document_type]}*\n\n"
            "यह document इसी existing case के facts से बनाया जाएगा।\n\n"
            f"{questions_text(state)}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=case_actions(state))
        return
    await update.message.reply_text(f"➕ {DISPLAY_NAMES[document_type]} इसी case की जानकारी से तैयार किया जा रहा है…")
    await generate_for(update, uid, state, edit=False)


async def add_document_command(update, context):
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state or not state.facts:
        await update.message.reply_text("पहले /newcase से एक case तैयार करें।", reply_markup=style_selection_menu())
        return
    await update.message.reply_text(
        "➕ *इसी केस में अगला दस्तावेज़ (Affidavit, Jawab Dava, Application आदि) चुनें:*",
        parse_mode="Markdown",
        reply_markup=document_menu(state),
    )


async def text_message(update, context):
    if not update.effective_user:
        return
    uid = update.effective_user.id
    raw_text = update.message.text or ""
    state = get_state(uid)

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

    await intake_text(update, context)


async def voice_message(update, context):
    if not update.message or not update.message.voice or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    state = get_state(uid)

    await update.message.reply_text("🎙️ Voice note मिला। Transcript तैयार किया जा रहा है…")
    try:
        f = await context.bot.get_file(update.message.voice.file_id)
        buf = BytesIO()
        await f.download_to_memory(out=buf)
        transcript = await asyncio.to_thread(transcribe_voice, buf.getvalue(), "telegram_voice.ogg")
        await update.message.reply_text(f"📝 *Transcript:*\n{transcript[:3500]}", parse_mode="Markdown")

        state = get_state(uid)
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

        await intake_text(update, context, transcript)
    except Exception:
        log.exception("voice failed")
        await update.message.reply_text("❌ Voice note process नहीं हो सका।")


async def image_document_message(update, context):
    """OCR a Telegram photo/image/PDF and feed extracted text + caption into semantic intake or edit mode."""
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    uid = update.effective_user.id
    caption = (update.message.caption or "").strip()
    state = get_state(uid)
    if not state:
        inferred = infer_document_type(caption, default="dava_plaint")
        state = CaseState(case_id_for(uid), document_type=inferred, style_mode="template_mode", style_selected=False)
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
                or content_type == "application/pdf"
                or filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".heif", ".pdf"))
            )
            if not supported:
                await update.message.reply_text(
                    "⚠️ अभी केवल image files (JPG/PNG/BMP/TIFF/HEIF) और PDF documents स्वीकार किए जाते हैं."
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

        await update.message.reply_text("📸 Document मिला। OCR और structure निकाली जा रही है…")
        extracted = await asyncio.to_thread(analyze_document_bytes, file_bytes, content_type, filename)
        text = extracted.text.strip()
        if caption:
            text = f"{caption}\n\n{text}".strip()
        if not text:
            await update.message.reply_text("⚠️ Document से readable text नहीं मिला। कृपया साफ़/सीधी photo भेजें।")
            return

        if state.draft and (state.pending_point_action or state.edit_mode or (caption and detect_edit_intent(caption)) or detect_edit_intent(text[:500])):
            state.edit_mode = True
            store.save(uid, state)
            await process_edit_instruction(update, context, caption or text)
            return

        page_count = extracted.page_count
        chunk_size = max(20000, int(os.getenv("IMAGE_INTAKE_CHUNK_CHARS", "60000")))
        chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
        if len(chunks) == 1:
            await update.message.reply_text(
                f"✅ OCR पूरा हुआ — {page_count} page{'s' if page_count != 1 else ''}, {len(text)} characters.\n\n"
                "🧠 अब extracted text से legal case facts समझे जा रहे हैं…"
            )
        else:
            await update.message.reply_text(
                f"✅ OCR पूरा हुआ — {page_count} pages, {len(text)} characters.\n"
                f"📚 Large document को {len(chunks)} safe chunks में process किया जाएगा।"
            )
        for index, chunk in enumerate(chunks, 1):
            if len(chunks) > 1 and index in {1, len(chunks)}:
                await update.message.reply_text(f"🧠 OCR semantic extraction {index}/{len(chunks)}…")
            await intake_text(update, context, chunk, suppress_questions=True, announce=False)

        state = get_state(uid)
        if state and not state.style_selected:
            await update.message.reply_text(
                f"✅ पूरे document से केस के तथ्य समझ लिए गए हैं!\n\n{summary_text(state)}\n"
                "👉 *अब चुनें कि आप किस Style से ड्राफ्ट तैयार करना चाहते हैं:*",
                parse_mode="Markdown",
                reply_markup=style_selection_menu(),
            )
        elif state and not missing_fields(state.facts, state.document_type):
            await update.message.reply_text(f"✅ पूरे document से आवश्यक case facts समझ लिए गए।\n\n{summary_text(state)}", parse_mode="Markdown", reply_markup=case_actions(state))
        else:
            await update.message.reply_text(f"📋 OCR पूरा हुआ। अभी केवल आवश्यक जानकारी पूछी जाएगी:\n\n{questions_text(state)}\n\nProgress: {progress(state)}", parse_mode="Markdown", reply_markup=case_actions(state))
    except DocumentIntelligenceError as exc:
        log.exception("Document Intelligence failed")
        await update.message.reply_text(f"❌ Image/document पढ़ा नहीं जा सका: {str(exc)[:500]}")
    except Exception:
        log.exception("image/document processing failed")
        await update.message.reply_text("❌ Image/document process नहीं हो सका। कृपया दोबारा भेजें।")


async def generate_for(update, uid, state, edit=False):
    target = update.callback_query.message if update.callback_query else update.message
    style_label = STYLE_NAMES.get(state.style_mode, state.style_mode)
    if edit:
        await update.callback_query.edit_message_text(
            f"⚖️ *{DISPLAY_NAMES[state.document_type]}*\n"
            f"🎨 *Style:* {style_label}\n\n"
            "⏳ Draft तैयार किया जा रहा है…\n\n"
            "1/4 Case details verified\n"
            "2/4 Best suited template matched from corpus\n"
            "3/4 Updating template with your case details & points\n"
            "4/4 Rendering canonical DOCX & court-standard PDF",
            parse_mode="Markdown",
        )
    else:
        await target.reply_text(f"⏳ *{DISPLAY_NAMES[state.document_type]}* ({style_label}) तैयार किया जा रहा है…", parse_mode="Markdown")
    try:
        draft = await asyncio.to_thread(orchestrator.draft_live, state)
        state.set_draft(draft)
        with tempfile.TemporaryDirectory() as tmp:
            label = DISPLAY_NAMES[state.document_type].split(" /")[0].replace(" ", "_")
            docx, pdf = render_both(draft, tmp, base_name=f"{label}_{uid}", paper=os.getenv("LEGAL_PAPER", "legal"))
            with open(docx, "rb") as f:
                await target.reply_document(f, filename=docx.name, caption=f"📄 Editable DOCX — {DISPLAY_NAMES[state.document_type]}")
            with open(pdf, "rb") as f:
                await target.reply_document(f, filename=pdf.name, caption=f"📑 Court-Ready PDF — {DISPLAY_NAMES[state.document_type]}")
        store.save(uid, state)
        tpl_info = f"📄 *Selected Template:* `{state.selected_template_name}`\n" if state.selected_template_name else ""
        await target.reply_text(
            f"✅ *Draft तैयार है!*\n"
            f"🎨 *Style:* {style_label}\n"
            f"{tpl_info}\n"
            "💡 *अगला कदम:*\n"
            "• कोई नया पॉइंट जोड़ने के लिए *➕ Add Point* दबाएं\n"
            "• कोई पॉइंट हटाने के लिए *🗑️ Remove Point* दबाएं\n"
            "• इसी केस का शपथपत्र, जवाब दावा या प्रार्थना पत्र बनाने के लिए *➕ Add Another Document* दबाएं\n"
            "• या सीधे बोलकर (Voice Note) / लिखकर कोई भी बदलाव बताएं!",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
    except Exception as exc:
        log.exception("generation failed")
        msg = "⏳ AI service अभी व्यस्त है। थोड़ी देर बाद फिर कोशिश करें।" if isinstance(exc, GeminiError) and exc.retryable else f"❌ Draft तैयार नहीं हो सका: {str(exc)[:500]}"
        await target.reply_text(msg)


async def enter_edit_mode(update, context):
    if not update.message or not update.effective_user:
        return
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state or not state.draft:
        await update.message.reply_text("अभी कोई generated draft उपलब्ध नहीं है। पहले draft generate करें।")
        return
    state.edit_mode = True
    state.pending_point_action = ""
    state.pending_edit = {}
    store.save(update.effective_user.id, state)
    await update.message.reply_text(
        "✏️ *DRAFT EDIT MODE*\n\n"
        "अपना बदलाव Hindi/English में लिखें, Voice Note भेजें, या फोटो भेजें।\n\n"
        "उदाहरण:\n"
        "• `इसमें यह पॉइंट और जोड़ दो कि प्रतिवादी ने 20,000 रुपये बयाना लिया था।`\n"
        "• `पैराग्राफ 4 वाला पॉइंट हटा दो।`\n"
        "• `पैराग्राफ 3 में 15.06.2026 की जगह 20.06.2026 कर दो।`\n"
        "• `प्रार्थना को दूसरे पेज पर ले जाओ।`\n\n"
        "AI केवल वही बदलाव करेगा और बाकी संपूर्ण ड्राफ्ट सुरक्षित रहेगा।",
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
        await update.message.reply_text("कोई generated draft उपलब्ध नहीं है। पहले draft generate करें।")
        return
    instruction = decode_krutidev_hybrid(instruction.strip())
    if not instruction:
        return
    state.edit_mode = True
    action_mode = state.pending_point_action
    state.pending_point_action = ""
    await update.message.reply_text("🔎 आपके निर्देश के अनुसार ड्राफ्ट अपडेट तैयार किया जा रहा है…")
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
        await update.message.reply_text(
            "🔎 *Proposed Changes:*\n\n" + preview + "\n\nकेवल यही बदलाव लागू होगा; बाकी संपूर्ण ड्राफ्ट सुरक्षित रहेगा।",
            parse_mode="Markdown",
            reply_markup=edit_actions(),
        )
    except Exception as exc:
        log.exception("edit parse/apply failed")
        await update.message.reply_text(f"⚠️ बदलाव apply करने योग्य exact target नहीं मिला। कृपया पैराग्राफ नंबर या शब्द स्पष्ट लिखें।\n\n{str(exc)[:400]}")


async def edit_text_message(update, context):
    await process_edit_instruction(update, context, update.message.text)


async def edit_voice_message(update, context):
    if not update.message or not update.message.voice or not update.effective_user:
        return
    if not await require_access(update):
        return
    state = get_state(update.effective_user.id)
    if not state or not state.draft:
        await update.message.reply_text("पहले एक draft तैयार करें।")
        return
    await update.message.reply_text("🎙️ Voice edit मिला। Transcript तैयार किया जा रहा है…")
    try:
        f = await context.bot.get_file(update.message.voice.file_id)
        buf = BytesIO()
        await f.download_to_memory(out=buf)
        transcript = await asyncio.to_thread(transcribe_voice, buf.getvalue(), "telegram_edit.ogg")
        await update.message.reply_text(f"📝 *Transcript:*\n{transcript[:3500]}", parse_mode="Markdown")
        await process_edit_instruction(update, context, transcript)
    except Exception:
        log.exception("voice edit failed")
        await update.message.reply_text("❌ Voice edit process नहीं हो सका।")


async def apply_edit(update, context):
    q = update.callback_query
    uid = q.from_user.id
    if not access.is_authorized(uid):
        await q.edit_message_text("🔒 यह bot private है।")
        return
    state = get_state(uid)
    pending = state.pending_edit if state else {}
    candidate = pending.get("candidate") if pending else None
    if not state or not candidate:
        await q.edit_message_text("⚠️ यह edit अब उपलब्ध नहीं है।")
        return

    state.set_draft(candidate)
    state.edit_mode = True
    store.save(uid, state)

    await q.edit_message_text("⏳ Change applied! Updated DOCX व PDF render किए जा रहे हैं…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            label = DISPLAY_NAMES[state.document_type].split(" /")[0].replace(" ", "_")
            docx, pdf = render_both(candidate, tmp, base_name=f"{label}_{uid}_v{state.draft_version}", paper=os.getenv("LEGAL_PAPER", "legal"))
            with open(docx, "rb") as f:
                await q.message.reply_document(f, filename=docx.name, caption=f"📄 Updated DOCX — v{state.draft_version}")
            with open(pdf, "rb") as f:
                await q.message.reply_document(f, filename=pdf.name, caption=f"📑 Updated PDF — v{state.draft_version}")
        await q.message.reply_text(
            f"✅ Edit applied (v{state.draft_version})! और कोई पॉइंट जोड़ना या हटाना हो तो नीचे बटन दबाएं या सीधे बोलें/लिखें।",
            reply_markup=case_actions(state),
        )
    except Exception:
        log.exception("render after edit failed")
        await q.message.reply_text("❌ Change save हुआ लेकिन updated document render नहीं हो सका। /summary से draft फिर खोलें।")


async def button(update, context):
    q = update.callback_query
    await q.answer()
    if not access.is_authorized(q.from_user.id):
        await q.edit_message_text("🔒 यह bot private है। आप authorized नहीं हैं। /myid से अपना User ID देखें।")
        return
    uid = q.from_user.id
    cid = case_id_for(uid)

    if q.data == "choose_style":
        await q.edit_message_text(
            STYLE_PROMPT_TEXT,
            parse_mode="Markdown",
            reply_markup=style_selection_menu(),
        )
        return

    if q.data.startswith("style:"):
        chosen_style = q.data.split(":", 1)[1]
        state = store.get(cid)
        if not state:
            state = CaseState(cid, document_type="dava_plaint", style_mode=chosen_style, style_selected=True)
        else:
            state.style_mode = chosen_style
            state.style_selected = True
        store.save(uid, state)
        style_label = STYLE_NAMES.get(chosen_style, chosen_style)

        # If facts are already ready, let the user generate right away or pick document type
        if state.facts and not missing_fields(state.facts, state.document_type):
            await q.edit_message_text(
                f"✅ *Style Selected:* {style_label}\n\n{summary_text(state)}\n"
                "अब आप सीधे *✨ Generate Draft* दबा सकते हैं या नीचे से दूसरा Document Type चुन सकते हैं:",
                parse_mode="Markdown",
                reply_markup=case_actions(state),
            )
            return

        await q.edit_message_text(
            f"✅ *Style Selected:* {style_label}\n\n"
            "अब नीचे से **Document Type** चुनें (या सीधे अपने केस का विवरण, पार्टी के नाम, कोर्ट परिसर और पिताजी का नाम Voice Note / Text / Photo में भेजें):",
            parse_mode="Markdown",
            reply_markup=dashboard(),
        )
        return

    if q.data == "switch_style":
        state = store.get(cid)
        cur = state.style_mode if state else "template_mode"
        await q.edit_message_text(
            "🔄 *Switch Drafting Style*\n\nइसी केस को दूसरे स्टाइल (500+ Templates ↔ Dad's 306 Files) में तैयार करने के लिए नीचे चुनें:",
            parse_mode="Markdown",
            reply_markup=switch_style_menu(cur),
        )
        return

    if q.data.startswith("regen_style:"):
        chosen_style = q.data.split(":", 1)[1]
        state = store.get(cid)
        if not state or not state.facts:
            await q.edit_message_text("पहले केस का विवरण भेजें।", reply_markup=style_selection_menu())
            return
        state.style_mode = chosen_style
        state.style_selected = True
        store.save(uid, state)
        await generate_for(update, uid, state, edit=True)
        return

    if q.data == "add_point_mode":
        state = store.get(cid)
        if not state or not state.draft:
            await q.edit_message_text("पहले एक ड्राफ्ट तैयार करें।")
            return
        state.edit_mode = True
        state.pending_point_action = "add"
        store.save(uid, state)
        await q.edit_message_text(
            "➕ *ADD POINT TO DRAFT (नया पॉइंट जोड़ें)*\n\n"
            "आप या आपके पिताजी इस ड्राफ्ट में जो भी अतिरिक्त बात, तथ्य या प्रार्थना बिंदु जोड़ना चाहते हैं, उसे सीधे लिखकर या Voice Note बोलकर भेजें।\n\n"
            "उदाहरण:\n"
            "• `यह पॉइंट जोड़ दो कि प्रतिवादी ने पंचों के सामने माफी मांगी थी।`\n"
            "• `प्रार्थना में कमीशन नियुक्त करने का पॉइंट जोड़ दो।`",
            parse_mode="Markdown",
        )
        return

    if q.data == "remove_point_mode":
        state = store.get(cid)
        if not state or not state.draft:
            await q.edit_message_text("पहले एक ड्राफ्ट तैयार करें।")
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
        await q.edit_message_text(
            "🗑️ *REMOVE POINT FROM DRAFT (पॉइंट हटाएं)*\n\n"
            f"वर्तमान पैराग्राफ:\n{para_block}\n\n"
            "आप जो भी पैराग्राफ या पॉइंट हटाना चाहते हैं, उसका नंबर या विवरण लिखकर या Voice Note से भेजें (जैसे: `पैराग्राफ 3 हटा दो` या `हर्जाने वाली बात हटा दो`)।",
            parse_mode="Markdown",
        )
        return

    if q.data == "case_documents":
        state = store.get(cid)
        if not state or not state.documents:
            await q.edit_message_text("इस case में अभी कोई generated document नहीं है।")
            return
        await q.edit_message_text("📚 इस case के generated documents:", reply_markup=existing_documents_menu(state))
        return

    if q.data.startswith("doc:"):
        state = store.get(cid)
        did = q.data.split(":", 1)[1]
        item = next((x for x in (state.documents if state else []) if x.get("document_id") == did), None)
        if not state or not item:
            await q.edit_message_text("⚠️ यह document अब उपलब्ध नहीं है।")
            return
        state.document_type = str(item.get("document_type"))
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
        await q.edit_message_text(
            f"📄 *{DISPLAY_NAMES[state.document_type]}* selected.\n"
            f"🎨 *Style:* {STYLE_NAMES.get(state.style_mode, state.style_mode)}\n\n"
            "अब इसे review/edit कर सकते हैं, पॉइंट जोड़/हटा सकते हैं या इसी case में दूसरा document बना सकते हैं।",
            parse_mode="Markdown",
            reply_markup=case_actions(state),
        )
        return

    if q.data == "add_document":
        state = store.get(cid)
        await q.edit_message_text(
            "➕ *इसी केस के विवरण से अगला दस्तावेज़ (Affidavit, Jawab Dava, Application आदि) चुनें:*",
            parse_mode="Markdown",
            reply_markup=document_menu(state),
        )
        return

    if q.data.startswith("addtype:"):
        t = q.data.split(":", 1)[1]
        state = store.get(cid)
        if not state or not state.facts:
            await q.edit_message_text("पहले एक case तैयार करें।", reply_markup=style_selection_menu())
            return
        if state.document_type == t and state.draft:
            await q.edit_message_text("यह document अभी active है। कोई दूसरा type चुनें।", reply_markup=document_menu(state))
            return
        state.begin_new_document(t)
        try:
            await asyncio.to_thread(orchestrator.refresh_intake, state)
            store.save(uid, state)
        except Exception:
            log.exception("additional document callback reconciliation failed")
        if missing_fields(state.facts, t):
            await q.edit_message_text(f"➕ *{DISPLAY_NAMES[t]}*\n\n{questions_text(state)}", parse_mode="Markdown", reply_markup=case_actions(state))
            return
        await q.edit_message_text(f"➕ *{DISPLAY_NAMES[t]}* इसी case के facts से तैयार किया जा रहा है…", parse_mode="Markdown")
        await generate_for(update, uid, state, edit=True)
        return

    if q.data.startswith("type:"):
        t = q.data.split(":", 1)[1]
        existing = store.get(cid)
        chosen_style = existing.style_mode if existing else "template_mode"
        style_sel = existing.style_selected if existing else True
        state = CaseState(cid, document_type=t, style_mode=chosen_style, style_selected=style_sel)
        store.save(uid, state)
        await q.edit_message_text(
            f"⚖️ *{DISPLAY_NAMES[t]}*\n\n"
            f"Progress: {progress(state)}\n\n"
            f"{questions_text(state)}\n\n"
            "आप Hindi/English text, Voice Note या दस्तावेज़ की Photo/PDF भेज सकते हैं।",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Change Style (Dad's ↔ 500+ Templates)", callback_data="choose_style")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]),
        )
        return

    state = store.get(cid)
    if q.data == "cancel":
        store.delete(cid)
        await q.edit_message_text("✅ Draft session cancelled.")
        return
    if not state:
        await q.edit_message_text("कोई active draft नहीं है। /newcase से शुरू करें।")
        return
    if q.data == "review":
        await q.edit_message_text(summary_text(state), parse_mode="Markdown", reply_markup=case_actions(state))
        return
    if q.data == "edit_mode":
        state.edit_mode = True
        state.pending_point_action = ""
        state.pending_edit = {}
        store.save(uid, state)
        await q.edit_message_text(
            "✏️ *DRAFT EDIT MODE*\n\nअब अपना बदलाव text में लिखें या Voice Note भेजें।\n\n"
            "उदाहरण:\n"
            "• `पैराग्राफ 3 में 15.06.2026 की जगह 20.06.2026 कर दो।`\n"
            "• `यह पॉइंट जोड़ दो कि ...`\n"
            "• `पैराग्राफ 4 हटा दो।`\n\n"
            "हर instruction पर पहले preview मिलेगा, फिर आप Apply/Discard चुनेंगे।",
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
        await q.edit_message_text("❌ Proposed change discarded. Original draft unchanged.", reply_markup=case_actions(state))
        return
    if q.data == "generate":
        try:
            await q.edit_message_text("🧠 पूरे case की जानकारी verify की जा रही है और सबसे उपयुक्त टेम्पलेट चुना जा रहा है…")
            await asyncio.to_thread(orchestrator.refresh_intake, state)
            store.save(uid, state)
        except Exception:
            log.exception("pre-generation intake reconciliation failed")
        if missing_fields(state.facts, state.document_type):
            await q.edit_message_text(questions_text(state), parse_mode="Markdown", reply_markup=case_actions(state))
            return
        await generate_for(update, uid, state, edit=True)


def build_application():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newcase", newcase))
    app.add_handler(CommandHandler("style", style_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("corpus", corpus_info))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("adddocument", add_document_command))
    app.add_handler(CommandHandler("myid", myid))
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
