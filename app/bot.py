"""Telegram entry point for the Legal Drafting Bot.

Phase 6A:
- conversational Dava/Plaint intake in Hindi/English
- Azure structured extraction for natural-language messages
- targeted missing-information questions
- Dava retrieval/composition
- deterministic DOCX + PDF generation
- persistent SQLite case state
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.case_store import CaseStore
from app.drafting_engine.conversation_state import CaseState
from app.drafting_engine.dava_orchestrator import DavaOrchestrator
from app.drafting_engine.renderers.legal_document_renderer import render_both

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("legal-bot")

store = CaseStore()
orchestrator = DavaOrchestrator()


def case_id_for(user_id: int) -> str:
    return f"tg-{user_id}-current"


def get_or_create_state(user_id: int) -> CaseState:
    case_id = case_id_for(user_id)
    state = store.get(case_id)
    if state is None:
        state = CaseState(case_id)
        store.save(user_id, state)
    return state


def _clean_value(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_simple_fact_input(text: str) -> dict[str, Any]:
    """Parse explicit labels without asking Azure to re-interpret them."""
    label_map = {
        "court_name": ["court", "न्यायालय"],
        "plaintiffs": ["plaintiff", "plaintiffs", "वादी", "वादीगण"],
        "defendants": ["defendant", "defendants", "प्रतिवादी", "प्रतिवादीगण"],
        "reliefs": ["relief", "reliefs", "राहत", "प्रार्थना"],
        "plaintiff_intro": ["plaintiff intro", "वादी परिचय"],
        "defendant_intro": ["defendant intro", "प्रतिवादी परिचय"],
        "property_description": ["property", "property description", "संपत्ति", "सम्पत्ति", "संपत्ति विवरण"],
        "valuation": ["valuation", "मूल्यांकन"],
        "court_fee": ["court fee", "न्यायालय शुल्क"],
        "cause_of_action": ["cause of action", "वाद-कारण", "वाद कारण"],
        "jurisdiction_facts": ["jurisdiction", "क्षेत्राधिकार"],
        "limitation_facts": ["limitation", "समय-सीमा", "समय सीमा"],
        "facts": ["facts", "तथ्य"],
        "interim_reliefs": ["interim relief", "अंतरिम राहत", "अंतरिम प्रार्थना"],
        "documents": ["documents", "दस्तावेज", "दस्तावेज़"],
        "place": ["place", "स्थान"],
        "date": ["date", "दिनांक"],
        "case_number": ["case number", "वाद संख्या", "वाद संख्‍या"],
        "case_year": ["case year", "वाद वर्ष"],
        "verification": ["verification", "सत्यापन"],
    }
    patterns = [(key, label) for key, labels in label_map.items() for label in labels]
    patterns.sort(key=lambda x: len(x[1]), reverse=True)
    pattern = re.compile(r"^\s*(?P<label>" + "|".join(re.escape(x[1]) for x in patterns) + r")\s*[:\-]\s*(?P<value>.*?)\s*$", re.I)
    out: dict[str, Any] = {}
    for line in text.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        raw = m.group("label").strip().casefold()
        value = _clean_value(m.group("value"))
        if not value:
            continue
        key = next(k for k, label in patterns if label.casefold() == raw)
        if key in {"plaintiffs", "defendants", "reliefs", "interim_reliefs", "documents"}:
            parts = [_clean_value(x) for x in re.split(r"[;,]", value) if _clean_value(x)]
            out[key] = parts or [value]
        elif key == "facts":
            out.setdefault("facts", []).append(value)
        else:
            out[key] = value
    return out


def _questions_message(questions: list[str]) -> str:
    return (
        "ठीक है। Draft को पर्याप्त रूप से पूरा करने के लिए अभी ये जानकारी चाहिए:\n\n"
        + "\n".join(questions)
        + "\n\nआप एक-एक करके या एक ही message में सभी उत्तर दे सकते हैं।"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    await update.message.reply_text(
        "नमस्ते। यह Legal Drafting Bot है।\n\n"
        "/newcase — नया वाद शुरू करें\n"
        "/cancel — वर्तमान case रद्द करें\n\n"
        "अब आप case को सामान्य भाषा में भी बता सकते हैं। उदाहरण:\n"
        "\"राम कुमार की जमीन पर श्याम कुमार ने कब्जा कर लिया है और स्थायी निषेधाज्ञा चाहिए।\"\n\n"
        "Bot आवश्यक जानकारी पूछेगा और अंत में DOCX + PDF Dava तैयार करेगा।"
    )


async def newcase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    state = CaseState(case_id_for(update.effective_user.id))
    store.save(update.effective_user.id, state)
    inline = " ".join(context.args).strip()
    if inline:
        state.record("user", inline)
        try:
            parsed = parse_simple_fact_input(inline)
            result = await asyncio.to_thread(orchestrator.extract_and_collect, state, inline) if not parsed else orchestrator.collect(state, parsed)
            store.save(update.effective_user.id, state)
            if result["status"] == "needs_information":
                await update.message.reply_text(_questions_message(result["questions"]))
            else:
                await update.message.reply_text(_ready_message(), reply_markup=_draft_keyboard())
            return
        except Exception:
            log.exception("Inline /newcase intake failed")
    await update.message.reply_text(
        "नया वाद शुरू हो गया है।\n\n"
        "अब आप मामले की जानकारी सामान्य भाषा में भेज सकते हैं।\n"
        "उदाहरण: \"राम कुमार की जमीन पर श्याम कुमार ने कब्जा किया है।\"\n\n"
        "या labelled format भी भेज सकते हैं: न्यायालय:, वादी:, प्रतिवादी:, तथ्य:, राहत:"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    store.delete(case_id_for(update.effective_user.id))
    await update.message.reply_text("वर्तमान case रद्द कर दिया गया है। /newcase से नया case शुरू करें।")


def _case_summary(state: CaseState) -> str:
    f = state.facts
    def val(key, default="—"):
        v = f.get(key)
        if isinstance(v, list):
            return "\n".join(f"• {x}" for x in v) if v else default
        return str(v).strip() if v else default
    return (
        "📋 *Case Review / मामले का सारांश*\n\n"
        f"*न्यायालय:* {val('court_name')}\n"
        f"*वादी:* {val('plaintiffs')}\n"
        f"*प्रतिवादी:* {val('defendants')}\n"
        f"*वादी परिचय:* {val('plaintiff_intro')}\n"
        f"*प्रतिवादी परिचय:* {val('defendant_intro')}\n"
        f"*संपत्ति:* {val('property_description')}\n"
        f"*वाद-कारण:* {val('cause_of_action')}\n"
        f"*क्षेत्राधिकार:* {val('jurisdiction_facts')}\n"
        f"*समय-सीमा:* {val('limitation_facts')}\n"
        f"*मूल्यांकन:* {val('valuation')}\n"
        f"*न्यायालय शुल्क:* {val('court_fee')}\n"
        f"*तथ्य:*\n{val('facts')}\n"
        f"*राहत:*\n{val('reliefs')}\n\n"
        "यदि कोई तथ्य गलत है, उसे स्पष्ट रूप से सुधारकर नया message भेजें।"
    )


def _ready_message(state: CaseState | None = None) -> str:
    return (
        "आवश्यक न्यूनतम जानकारी मिल गई है।\n\n"
        "Draft बनाने से पहले case summary जाँच लें।\n"
        "कोई तथ्य गलत हो तो नया message भेजकर स्पष्ट correction दें; फिर /summary से दोबारा जाँच सकते हैं।"
    )


def _draft_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Dava Draft तैयार करें", callback_data="draft")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_user:
        return
    state = get_or_create_state(update.effective_user.id)
    text = update.message.text.strip()
    state.record("user", text)

    parsed = parse_simple_fact_input(text)
    try:
        if parsed:
            result = orchestrator.collect(state, parsed)
        else:
            await update.message.reply_text("ठीक है, मैं इस message से case की उपलब्ध जानकारी निकाल रहा हूँ…")
            result = await asyncio.to_thread(orchestrator.extract_and_collect, state, text)
        store.save(update.effective_user.id, state)
    except Exception as exc:
        log.exception("Intake failed")
        await update.message.reply_text(
            "इस message को process नहीं कर सका। कृपया फिर से भेजें।\n"
            "कृपया message दोबारा भेजें या /newcase से नया case शुरू करें।"
        )
        return

    if result["status"] == "needs_information":
        await update.message.reply_text(_questions_message(result["questions"]))
        return

    facts = state.facts
    summary = (
        f"{_ready_message()}\n\n"
        f"वादी: {', '.join(map(str, facts.get('plaintiffs', [])))}\n"
        f"प्रतिवादी: {', '.join(map(str, facts.get('defendants', [])))}\n"
        f"तथ्य: {len(facts.get('facts', []))} item(s)\n"
        f"राहत: {len(facts.get('reliefs', []))} item(s)"
    )
    await update.message.reply_text(summary, reply_markup=_draft_keyboard())


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    state = store.get(case_id_for(update.effective_user.id))
    if state is None:
        await update.message.reply_text("कोई active case नहीं है। /newcase से शुरू करें।")
        return
    await update.message.reply_text(_case_summary(state), parse_mode="Markdown", reply_markup=_draft_keyboard())


async def redraft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    state = store.get(case_id_for(update.effective_user.id))
    if state is None:
        await update.message.reply_text("कोई active case नहीं है। /newcase से शुरू करें।")
        return
    if state.status not in {"ready", "drafted"}:
        await update.message.reply_text("Case अभी पूरा नहीं है। पहले आवश्यक जानकारी दें।")
        return
    await update.message.reply_text("ठीक है। Case की वर्तमान जानकारी से नया Dava draft बनाया जा रहा है…")
    try:
        draft = await asyncio.to_thread(orchestrator.draft_live, state)
        with tempfile.TemporaryDirectory() as tmp:
            base = f"Dava_Draft_{update.effective_user.id}"
            docx_path, pdf_path = render_both(draft, tmp, base_name=base, paper=os.getenv("LEGAL_PAPER", "legal"))
            with open(docx_path, "rb") as f:
                await update.message.reply_document(f, filename=docx_path.name, caption="📄 Editable DOCX Dava")
            with open(pdf_path, "rb") as f:
                await update.message.reply_document(f, filename=pdf_path.name, caption="📑 PDF Dava")
        state.status = "drafted"
        store.save(update.effective_user.id, state)
        await update.message.reply_text("✅ नया Dava draft तैयार है।")
    except Exception:
        log.exception("Redraft failed")
        await update.message.reply_text("Draft generate नहीं हो सका। कृपया थोड़ी देर बाद फिर कोशिश करें।")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    await query.answer()
    case_id = case_id_for(query.from_user.id)
    state = store.get(case_id)

    if query.data == "cancel":
        store.delete(case_id)
        await query.edit_message_text("Case cancelled.")
        return
    if query.data != "draft":
        return
    if state is None:
        await query.edit_message_text("कोई active case नहीं है। /newcase से शुरू करें।")
        return

    await query.edit_message_text("Dava तैयार किया जा रहा है…")
    try:
        draft = await asyncio.to_thread(orchestrator.draft_live, state)
        with tempfile.TemporaryDirectory() as tmp:
            base = f"Dava_Draft_{query.from_user.id}"
            docx_path, pdf_path = render_both(draft, tmp, base_name=base, paper=os.getenv("LEGAL_PAPER", "legal"))
            with open(docx_path, "rb") as f:
                await query.message.reply_document(f, filename=docx_path.name, caption="📄 Editable DOCX Dava")
            with open(pdf_path, "rb") as f:
                await query.message.reply_document(f, filename=pdf_path.name, caption="📑 PDF Dava")
        state.status = "drafted"
        store.save(query.from_user.id, state)
        await query.message.reply_text("✅ Dava तैयार है। DOCX और PDF दोनों भेज दिए गए हैं।")
    except Exception as exc:
        log.exception("Draft failed")
        await query.message.reply_text(
            "Draft generate नहीं हो सका। Azure configuration या document renderer जाँचें.\n"
            "कृपया message दोबारा भेजें या /newcase से नया case शुरू करें।"
        )


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("newcase", newcase))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("summary", summary))
    application.add_handler(CommandHandler("redraft", redraft))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    return application


def main() -> None:
    application = build_application()
    log.info("Legal Drafting Bot starting.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
