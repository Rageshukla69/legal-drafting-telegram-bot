"""Professional Telegram UI for all seven legal draft types."""
from __future__ import annotations
import asyncio, logging, os, re, tempfile
from io import BytesIO
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from app.case_store import CaseStore
from app.drafting_engine.conversation_state import CaseState, DISPLAY_NAMES, missing_fields, next_questions
from app.drafting_engine.multi_draft_orchestrator import MultiDraftOrchestrator
from app.drafting_engine.renderers.legal_document_renderer import render_both
from app.drafting_engine.azure_speech import transcribe_voice
from app.drafting_engine.gemini_client import GeminiError

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log=logging.getLogger("legal-bot")
store=CaseStore(); orchestrator=MultiDraftOrchestrator()

TYPE_ORDER=["dava_plaint","written_statement","application","evidence_pw_affidavit","affidavit","legal_notice","other_civil"]
TYPE_ICONS={"dava_plaint":"📜","written_statement":"📝","application":"📄","evidence_pw_affidavit":"⚖️","affidavit":"📋","legal_notice":"📬","other_civil":"📑"}

def case_id_for(user_id:int)->str: return f"tg-{user_id}-current"
def get_state(uid:int): return store.get(case_id_for(uid))

def dashboard():
    rows=[]
    for i in range(0,len(TYPE_ORDER),2):
        row=[]
        for t in TYPE_ORDER[i:i+2]: row.append(InlineKeyboardButton(f"{TYPE_ICONS[t]} {DISPLAY_NAMES[t].split(' / ')[0]}",callback_data=f"type:{t}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def case_actions(state):
    rows=[[InlineKeyboardButton("✨ Generate Draft",callback_data="generate"),InlineKeyboardButton("👁 Review",callback_data="review")], [InlineKeyboardButton("🗑 Cancel",callback_data="cancel")]]
    return InlineKeyboardMarkup(rows)

def progress(state):
    missing=missing_fields(state.facts,state.document_type)
    total=len(missing_fields({},state.document_type)); done=max(0,total-len(missing)); pct=int(done/total*100) if total else 100
    filled="█"*max(1,pct//10); empty="░"*(10-len(filled))
    return f"{filled}{empty} {pct}%"

def questions_text(state):
    qs=next_questions(state.facts,state.document_type)
    if not qs: return "सारी आवश्यक जानकारी मिल गई है।"
    return "अभी इन जानकारी की आवश्यकता है:\n\n"+"\n".join(f"• {label}" for _,label in qs)+"\n\nआप एक message में एक या कई उत्तर भेज सकते हैं।"

def summary_text(state):
    facts=state.facts
    def val(k):
        v=facts.get(k,"—"); return "\n".join(f"• {x}" for x in v) if isinstance(v,list) else str(v)
    return (f"⚖️ *{DISPLAY_NAMES[state.document_type]}*\n\n*Progress:* {progress(state)}\n\n"
            f"*न्यायालय:* {val('court_name')}\n*पक्षकार:* {val('parties') or val('plaintiffs')}\n"
            f"*मुख्य तथ्य:* {val('facts')}\n*राहत/मांग:* {val('reliefs') if facts.get('reliefs') else val('demands')}\n")

async def start(update,context):
    if not update.message:return
    await update.message.reply_text("⚖️ *LEGAL DRAFTING WORKSPACE*\n\nसात प्रकार के legal drafts तैयार करें। नीचे document type चुनें।",parse_mode="Markdown",reply_markup=dashboard())

async def newcase(update,context):
    if not update.message or not update.effective_user:return
    await update.message.reply_text("⚖️ *नया Legal Draft*\n\nपहले document type चुनें:",parse_mode="Markdown",reply_markup=dashboard())

async def cancel(update,context):
    if not update.message or not update.effective_user:return
    store.delete(case_id_for(update.effective_user.id)); await update.message.reply_text("✅ Current draft session बंद कर दी गई है। /newcase से नया draft शुरू करें।")

async def summary(update,context):
    if not update.message or not update.effective_user:return
    state=get_state(update.effective_user.id)
    if not state: await update.message.reply_text("कोई active draft नहीं है। /newcase दबाएँ।"); return
    await update.message.reply_text(summary_text(state),parse_mode="Markdown",reply_markup=case_actions(state))

async def intake_text(update,context,text=None):
    if not update.effective_user:return
    text=(text or update.message.text).strip(); state=get_state(update.effective_user.id)
    if not state:
        await update.message.reply_text("पहले /newcase दबाकर document type चुनें।",reply_markup=dashboard()); return
    state.record("user",text); await update.message.reply_text(f"⏳ {DISPLAY_NAMES[state.document_type]} के लिए जानकारी समझी जा रही है…")
    try:
        result=await asyncio.to_thread(orchestrator.extract_and_collect,state,text); store.save(update.effective_user.id,state)
    except Exception as exc:
        log.exception("Intake failed"); await update.message.reply_text("❌ जानकारी process नहीं हो सकी। कृपया दोबारा भेजें।"); return
    if result["status"]=="needs_information":
        await update.message.reply_text(f"{questions_text(state)}\n\nProgress: {progress(state)}",reply_markup=case_actions(state)); return
    await update.message.reply_text(f"✅ आवश्यक जानकारी मिल गई।\n\n{summary_text(state)}",parse_mode="Markdown",reply_markup=case_actions(state))

async def text_message(update,context): await intake_text(update,context)

async def voice_message(update,context):
    if not update.message or not update.message.voice or not update.effective_user:return
    state=get_state(update.effective_user.id)
    if not state: await update.message.reply_text("पहले /newcase से document type चुनें।",reply_markup=dashboard()); return
    await update.message.reply_text("🎙️ Voice note मिला। Transcript तैयार किया जा रहा है…")
    try:
        f=await context.bot.get_file(update.message.voice.file_id); buf=BytesIO(); await f.download_to_memory(out=buf)
        transcript=await asyncio.to_thread(transcribe_voice,buf.getvalue(),"telegram_voice.ogg")
        await update.message.reply_text("📝 Transcript तैयार है। अब legal facts extract किए जा रहे हैं…")
        await intake_text(update,context,transcript)
    except Exception: log.exception("voice failed"); await update.message.reply_text("❌ Voice note process नहीं हो सका।")

async def generate_for(update,uid,state,edit=False):
    target=update.callback_query.message if update.callback_query else update.message
    if edit: await update.callback_query.edit_message_text(f"⚖️ *{DISPLAY_NAMES[state.document_type]}*\n\n⏳ Draft तैयार किया जा रहा है…\n\n1/4 Case verified\n2/4 Advocate examples selected\n3/4 Drafting & validation\n4/4 DOCX/PDF rendering",parse_mode="Markdown")
    else: await target.reply_text("⏳ Draft तैयार किया जा रहा है…")
    try:
        draft=await asyncio.to_thread(orchestrator.draft_live,state)
        with tempfile.TemporaryDirectory() as tmp:
            label=DISPLAY_NAMES[state.document_type].split(" /")[0].replace(" ","_")
            docx,pdf=render_both(draft,tmp,base_name=f"{label}_{uid}",paper=os.getenv("LEGAL_PAPER","legal"))
            with open(docx,"rb") as f: await target.reply_document(f,filename=docx.name,caption=f"📄 Editable DOCX — {DISPLAY_NAMES[state.document_type]}")
            with open(pdf,"rb") as f: await target.reply_document(f,filename=pdf.name,caption=f"📑 PDF — {DISPLAY_NAMES[state.document_type]}")
        store.save(uid,state); await target.reply_text("✅ Draft तैयार है। आप /summary से case review कर सकते हैं या फिर से Generate कर सकते हैं।",reply_markup=case_actions(state))
    except Exception as exc:
        log.exception("generation failed")
        msg="⏳ AI service अभी व्यस्त है। थोड़ी देर बाद फिर कोशिश करें।" if isinstance(exc,GeminiError) and exc.retryable else f"❌ Draft तैयार नहीं हो सका: {str(exc)[:500]}"
        await target.reply_text(msg)

async def button(update,context):
    q=update.callback_query; await q.answer()
    uid=q.from_user.id; cid=case_id_for(uid)
    if q.data.startswith("type:"):
        t=q.data.split(":",1)[1]; state=CaseState(cid,document_type=t); store.save(uid,state)
        await q.edit_message_text(f"⚖️ *{DISPLAY_NAMES[t]}*\n\nProgress: {progress(state)}\n\n{questions_text(state)}\n\nआप Hindi/English text या voice note भेज सकते हैं।",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="cancel")]])); return
    state=store.get(cid)
    if q.data=="cancel": store.delete(cid); await q.edit_message_text("✅ Draft session cancelled."); return
    if not state: await q.edit_message_text("कोई active draft नहीं है। /newcase से शुरू करें।"); return
    if q.data=="review": await q.edit_message_text(summary_text(state),parse_mode="Markdown",reply_markup=case_actions(state)); return
    if q.data=="generate":
        if missing_fields(state.facts,state.document_type): await q.edit_message_text(questions_text(state)); return
        await generate_for(update,uid,state,edit=True)

def build_application():
    token=os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    app=Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("newcase",newcase)); app.add_handler(CommandHandler("cancel",cancel)); app.add_handler(CommandHandler("summary",summary))
    app.add_handler(CallbackQueryHandler(button)); app.add_handler(MessageHandler(filters.VOICE,voice_message)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_message))
    return app

def main(): build_application().run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=="__main__": main()
