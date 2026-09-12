from __future__ import annotations
import logging, os, tempfile
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
from app.case_store import CaseStore
from app.drafting_engine.conversation_state import CaseState
from app.drafting_engine.dava_orchestrator import DavaOrchestrator
from app.drafting_engine.azure_speech import transcribe as transcribe_voice

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
    await update.message.reply_text("नमस्ते। /newcase से नया वाद शुरू करें। आप तथ्य text या Hindi voice note में भेज सकते हैं।")

async def newcase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = CaseState(case_id_for(update.effective_user.id))
    store.save(update.effective_user.id, state)
    await update.message.reply_text("नया वाद शुरू हो गया है। मामले की जानकारी सामान्य भाषा में text या voice note से भेजें।")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store.delete(case_id_for(update.effective_user.id))
    await update.message.reply_text("वर्तमान case रद्द कर दिया गया है। /newcase से नया case शुरू करें।")

async def handle_input(update: Update, text: str):
    state = get_or_create_state(update.effective_user.id)
    state.record("user", text)
    try:
        extracted = orchestrator.ingest(state, text)
        ready, missing = orchestrator.ready(state)
        store.save(update.effective_user.id, state)
        unclear = extracted.get("unclear_items", [])
        if not ready:
            labels = {"court_name":"न्यायालय", "plaintiffs":"वादी", "defendants":"प्रतिवादी", "facts":"मुख्य तथ्य", "reliefs":"मांगी गई राहत"}
            q = "\n".join(f"• {labels.get(x,x)}" for x in missing)
            extra = ("\n\nस्पष्ट नहीं मिली जानकारी:\n" + "\n".join("• "+x for x in unclear[:5])) if unclear else ""
            await update.message.reply_text("अभी ये जानकारी चाहिए:\n\n" + q + extra)
            return
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Draft तैयार करें", callback_data="draft")], [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
        await update.message.reply_text("आवश्यक जानकारी मिल गई है। Draft बनाने से पहले तथ्यों की जाँच कर लें।", reply_markup=keyboard)
    except Exception as exc:
        log.exception("Input processing failed")
        await update.message.reply_text(f"जानकारी process नहीं हो सकी। Technical detail: {type(exc).__name__}")

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await handle_input(update, update.message.text.strip())

async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return
    status = await update.message.reply_text("🎙️ Voice note को Hindi में transcribe किया जा रहा है…")
    try:
        tg_file = await context.bot.get_file(update.message.voice.file_id)
        data = await tg_file.download_as_bytearray()
        transcript = transcribe_voice(bytes(data), suffix=".ogg")
        await status.edit_text("📝 Transcription:\n\n" + transcript[:3800])
        await handle_input(update, transcript)
    except Exception as exc:
        log.exception("Voice transcription failed")
        await status.edit_text(f"Voice transcription failed: {type(exc).__name__}")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = store.get(case_id_for(update.effective_user.id))
    if query.data == "cancel":
        store.delete(case_id_for(update.effective_user.id)); await query.edit_message_text("Case cancelled."); return
    if query.data == "draft":
        if not state:
            await query.edit_message_text("कोई active case नहीं है। /newcase से शुरू करें।"); return
        await query.edit_message_text("Draft तैयार किया जा रहा है… Gemini + father's corpus + validator + Python renderer चल रहे हैं।")
        try:
            out = Path(tempfile.gettempdir()) / f"legalbot-{update.effective_user.id}"
            result = orchestrator.draft_live(state, out)
            state.status = "drafted"; store.save(update.effective_user.id, state)
            await query.message.reply_document(open(result["docx"], "rb"), caption="DOCX — Gemini draft + deterministic Python formatting")
            if result.get("pdf"):
                await query.message.reply_document(open(result["pdf"], "rb"), caption="PDF — generated from the same formatted DOCX")
        except Exception as exc:
            log.exception("Draft failed")
            await query.message.reply_text(f"Draft generate नहीं हो सका: {type(exc).__name__}. Corpus index और credentials जाँचें।")

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("newcase", newcase)); app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.VOICE, voice_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.run_polling()

if __name__ == "__main__": main()
