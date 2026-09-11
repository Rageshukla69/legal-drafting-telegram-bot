from __future__ import annotations
import logging, os, uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters

from app.case_store import CaseStore
from drafting_engine.conversation_state import CaseState
from drafting_engine.dava_orchestrator import DavaOrchestrator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("legal-bot")

store = CaseStore()
orchestrator = DavaOrchestrator()

def case_id_for(user_id: int) -> str:
    return f"tg-{user_id}-current"

def get_or_create_state(user_id: int) -> CaseState:
    cid = case_id_for(user_id)
    state = store.get(cid)
    if state is None:
        state = CaseState(cid)
        store.save(user_id, state)
    return state

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "नमस्ते। यह Legal Drafting Bot का पहला test version है।\n\n"
        "/newcase — नया वाद शुरू करें\n"
        "/cancel — वर्तमान case रद्द करें\n\n"
        "अभी text-based Dava/Plaint workflow सक्रिय है।"
    )

async def newcase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = case_id_for(update.effective_user.id)
    state = CaseState(cid)
    store.save(update.effective_user.id, state)
    await update.message.reply_text(
        "नया वाद शुरू हो गया है। आप मामले की जानकारी सामान्य भाषा में भेज सकते हैं।\n\n"
        "उदाहरण: वादी कौन है, प्रतिवादी कौन है, विवाद क्या है और क्या राहत चाहिए—जो पता हो भेजें।"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store.delete(case_id_for(update.effective_user.id))
    await update.message.reply_text("वर्तमान case रद्द कर दिया गया है। /newcase से नया case शुरू करें।")

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    state = get_or_create_state(update.effective_user.id)
    text = update.message.text.strip()
    state.record("user", text)

    # Phase 5 first milestone: accept explicitly structured facts.
    # Natural-language extraction will be connected to Azure in the next adapter.
    parsed = parse_simple_fact_input(text)
    result = orchestrator.collect(state, parsed)
    store.save(update.effective_user.id, state)

    if result["status"] == "needs_information":
        q = "\n".join(result["questions"])
        await update.message.reply_text(
            "जानकारी अभी पूरी नहीं है। कृपया इन बातों का उत्तर दें:\n\n" + q
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Draft तैयार करें", callback_data="draft")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])
    await update.message.reply_text(
        "आवश्यक न्यूनतम जानकारी मिल गई है। Draft बनाने से पहले कृपया तथ्यों की जाँच करें।",
        reply_markup=keyboard
    )

def parse_simple_fact_input(text: str) -> dict:
    """Safe starter parser.

    It only recognizes explicit labels. It never guesses unlabelled names or facts.
    Later this will be replaced/augmented by Azure Structured Outputs.
    """
    out = {}
    patterns = {
        "court_name": r"(?:court|न्यायालय)\s*[:\-]\s*(.+)",
        "plaintiffs": r"(?:plaintiff|वादी)\s*[:\-]\s*(.+)",
        "defendants": r"(?:defendant|प्रतिवादी)\s*[:\-]\s*(.+)",
        "reliefs": r"(?:relief|राहत|प्रार्थना)\s*[:\-]\s*(.+)",
        "plaintiff_intro": r"(?:plaintiff intro|वादी परिचय)\s*[:\-]\s*(.+)",
        "valuation": r"(?:valuation|मूल्यांकन)\s*[:\-]\s*(.+)",
        "court_fee": r"(?:court fee|न्यायालय शुल्क)\s*[:\-]\s*(.+)",
        "cause_of_action": r"(?:cause of action|वाद.?कारण)\s*[:\-]\s*(.+)",
        "jurisdiction_facts": r"(?:jurisdiction|क्षेत्राधिकार)\s*[:\-]\s*(.+)",
        "facts": r"(?:facts|तथ्य)\s*[:\-]\s*(.+)",
    }
    for key, pat in patterns.items():
        m = __import__("re").search(pat, text, __import__("re").I)
        if m:
            val = m.group(1).strip()
            out[key] = [val] if key in ("plaintiffs","defendants","reliefs") else (
                [{"number": 1, "text": val}] if key == "facts" else val
            )
    return out

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = store.get(case_id_for(update.effective_user.id))
    if query.data == "cancel":
        store.delete(case_id_for(update.effective_user.id))
        await query.edit_message_text("Case cancelled.")
        return
    if query.data == "draft":
        if not state:
            await query.edit_message_text("कोई active case नहीं है। /newcase से शुरू करें।")
            return
        await query.edit_message_text("Draft तैयार किया जा रहा है…")
        try:
            draft = orchestrator.draft_live(state)
            # For now, send structured draft as JSON text.
            # DOCX rendering is the next integration step.
            import json
            text = json.dumps(draft, ensure_ascii=False, indent=2)
            await query.message.reply_text("Structured Dava draft:\n\n" + text[:3900])
            state.status = "drafted"
            store.save(update.effective_user.id, state)
        except Exception as exc:
            log.exception("Draft failed")
            await query.message.reply_text(
                "Draft अभी generate नहीं हो सका। Configuration/credentials जाँचें।\n"
                f"Technical detail: {type(exc).__name__}"
            )

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newcase", newcase))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.run_polling()

if __name__ == "__main__":
    main()
