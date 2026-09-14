"""Professional Telegram UI for all seven legal draft types."""
from __future__ import annotations
import asyncio, logging, os, re, tempfile
from io import BytesIO
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from app.case_store import CaseStore
from app.access_control import AccessController
from app.drafting_engine.conversation_state import CaseState, DISPLAY_NAMES, missing_fields, next_questions
from app.drafting_engine.multi_draft_orchestrator import MultiDraftOrchestrator
from app.drafting_engine.renderers.legal_document_renderer import render_both
from app.drafting_engine.azure_speech import transcribe_voice
from app.drafting_engine.gemini_client import GeminiError
from app.drafting_engine.draft_editor import DraftEditor

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log=logging.getLogger("legal-bot")
store=CaseStore(); access=AccessController(store); orchestrator=MultiDraftOrchestrator(); editor=DraftEditor()

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
    rows=[[InlineKeyboardButton("✨ Generate Draft",callback_data="generate"),InlineKeyboardButton("👁 Review",callback_data="review")]]
    if state.draft:
        rows.append([InlineKeyboardButton("✏️ Edit Draft",callback_data="edit_mode")])
    rows.append([InlineKeyboardButton("🗑 Cancel",callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

def edit_actions():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Apply Change",callback_data="edit_apply"),InlineKeyboardButton("❌ Discard",callback_data="edit_discard")]])

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
    lines = [f"👑 Owner: `{access.owner_id}`", "", "*Authorized users:*" ]
    lines.extend(f"• `{u['user_id']}`" for u in users)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def start(update,context):
    if not update.message:return
    if not await require_access(update): return
    await update.message.reply_text("⚖️ *LEGAL DRAFTING WORKSPACE*\n\nसात प्रकार के legal drafts तैयार करें। नीचे document type चुनें।",parse_mode="Markdown",reply_markup=dashboard())

async def newcase(update,context):
    if not update.message or not update.effective_user:return
    if not await require_access(update): return
    await update.message.reply_text("⚖️ *नया Legal Draft*\n\nपहले document type चुनें:",parse_mode="Markdown",reply_markup=dashboard())

async def cancel(update,context):
    if not update.message or not update.effective_user:return
    if not await require_access(update): return
    store.delete(case_id_for(update.effective_user.id)); await update.message.reply_text("✅ Current draft session बंद कर दी गई है। /newcase से नया draft शुरू करें।")

async def summary(update,context):
    if not update.message or not update.effective_user:return
    if not await require_access(update): return
    state=get_state(update.effective_user.id)
    if not state: await update.message.reply_text("कोई active draft नहीं है। /newcase दबाएँ।"); return
    await update.message.reply_text(summary_text(state),parse_mode="Markdown",reply_markup=case_actions(state))

async def intake_text(update,context,text=None):
    if not update.effective_user:return
    if not access.is_authorized(update.effective_user.id):
        await deny(update); return
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

async def text_message(update,context):
    if not update.effective_user: return
    state=get_state(update.effective_user.id)
    if state and state.edit_mode and state.draft:
        await edit_text_message(update,context)
    else:
        await intake_text(update,context)

async def voice_message(update,context):
    if not update.message or not update.message.voice or not update.effective_user:return
    if not await require_access(update): return
    state=get_state(update.effective_user.id)
    if state and state.edit_mode and state.draft:
        await edit_voice_message(update,context); return
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
        store.save(uid,state); await target.reply_text("✅ Draft तैयार है। आप ✏️ Edit Draft दबाकर text/voice instruction से targeted बदलाव कर सकते हैं। बाकी सामग्री जस की तस रहेगी।",reply_markup=case_actions(state))
    except Exception as exc:
        log.exception("generation failed")
        msg="⏳ AI service अभी व्यस्त है। थोड़ी देर बाद फिर कोशिश करें।" if isinstance(exc,GeminiError) and exc.retryable else f"❌ Draft तैयार नहीं हो सका: {str(exc)[:500]}"
        await target.reply_text(msg)

async def enter_edit_mode(update, context):
    if not update.message or not update.effective_user: return
    if not await require_access(update): return
    state=get_state(update.effective_user.id)
    if not state or not state.draft:
        await update.message.reply_text("अभी कोई generated draft उपलब्ध नहीं है। पहले draft generate करें।")
        return
    state.edit_mode=True; state.pending_edit={}; store.save(update.effective_user.id,state)
    await update.message.reply_text(
        "✏️ *DRAFT EDIT MODE*\n\n"
        "अपना exact बदलाव Hindi/English में लिखें या voice note भेजें।\n\n"
        "उदाहरण:\n"
        "• `पैराग्राफ 3 में 15.06.2026 की जगह 20.06.2026 कर दो।`\n"
        "• `पैराग्राफ 5 की यह लाइन हटा दो: ...`\n"
        "• `पैराग्राफ 4 के बाद यह वाक्य जोड़ दो: ...`\n\n"
        "AI केवल एक targeted change प्रस्तावित करेगा। Apply करने से पहले आपको preview मिलेगा।",
        parse_mode="Markdown",
    )

async def edit_command(update, context):
    await enter_edit_mode(update, context)

async def process_edit_instruction(update, context, instruction: str):
    uid=update.effective_user.id
    if not access.is_authorized(uid): await deny(update); return
    state=get_state(uid)
    if not state or not state.draft:
        await update.message.reply_text("कोई generated draft उपलब्ध नहीं है। पहले draft generate करें।"); return
    if not state.edit_mode:
        await update.message.reply_text("Draft edit करने के लिए पहले /summary में जाकर ✏️ Edit Draft दबाएँ।"); return
    instruction=instruction.strip()
    if not instruction: return
    await update.message.reply_text("🔎 आपके edit instruction का exact target खोजा जा रहा है…")
    try:
        edit=await asyncio.to_thread(editor.parse,state.draft,instruction)
        if not edit.get("old_text") and edit.get("operation") in {"replace","delete","insert_before","insert_after"}:
            await update.message.reply_text("⚠️ यह बदलाव पर्याप्त स्पष्ट नहीं है। कृपया paragraph/section और बदलने वाला exact text बताएं।")
            return
        candidate, _ = await asyncio.to_thread(editor.apply,state.draft,edit)
        state.pending_edit={"edit":edit,"candidate":candidate,"instruction":instruction}
        store.save(uid,state)
        preview=editor.describe(edit)
        await update.message.reply_text("🔎 Proposed change\n\n"+preview+"\n\nकेवल यही targeted change लागू होगा; बाकी structured draft बदला नहीं जाएगा।",reply_markup=edit_actions())
    except Exception as exc:
        log.exception("edit parse/apply failed")
        await update.message.reply_text(f"⚠️ बदलाव apply करने योग्य exact target नहीं मिला। कोई बदलाव नहीं किया गया।\n\n{str(exc)[:400]}")

async def edit_text_message(update, context):
    await process_edit_instruction(update,context,update.message.text)

async def edit_voice_message(update, context):
    if not update.message or not update.message.voice or not update.effective_user:return
    if not await require_access(update): return
    state=get_state(update.effective_user.id)
    if not state or not state.draft or not state.edit_mode:
        await update.message.reply_text("पहले generated draft में ✏️ Edit Draft mode शुरू करें।"); return
    await update.message.reply_text("🎙️ Voice edit मिला। Transcript तैयार किया जा रहा है…")
    try:
        f=await context.bot.get_file(update.message.voice.file_id); buf=BytesIO(); await f.download_to_memory(out=buf)
        transcript=await asyncio.to_thread(transcribe_voice,buf.getvalue(),"telegram_edit.ogg")
        await update.message.reply_text("📝 Transcript:\n\n"+transcript[:3500])
        await process_edit_instruction(update,context,transcript)
    except Exception:
        log.exception("voice edit failed"); await update.message.reply_text("❌ Voice edit process नहीं हो सका।")

async def apply_edit(update, context):
    q=update.callback_query; uid=q.from_user.id
    if not access.is_authorized(uid): await q.edit_message_text("🔒 यह bot private है।"); return
    state=get_state(uid)
    pending=state.pending_edit if state else {}
    candidate=pending.get("candidate") if pending else None
    if not state or not candidate:
        await q.edit_message_text("⚠️ यह edit अब उपलब्ध नहीं है।"); return
    state.draft=candidate
    state.draft_version += 1
    state.draft_versions.append({"version":state.draft_version,"draft":candidate})
    state.draft_versions=state.draft_versions[-10:]
    state.pending_edit={}; state.edit_mode=False; state.status="drafted"
    store.save(uid,state)
    await q.edit_message_text("⏳ Change applied. DOCX/PDF को फिर से render किया जा रहा है…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            label=DISPLAY_NAMES[state.document_type].split(" /")[0].replace(" ","_")
            docx,pdf=render_both(candidate,tmp,base_name=f"{label}_{uid}_v{state.draft_version}",paper=os.getenv("LEGAL_PAPER","legal"))
            with open(docx,"rb") as f: await q.message.reply_document(f,filename=docx.name,caption=f"📄 Updated editable DOCX — v{state.draft_version}")
            with open(pdf,"rb") as f: await q.message.reply_document(f,filename=pdf.name,caption=f"📑 Updated PDF — v{state.draft_version}")
        await q.message.reply_text("✅ Edit applied. बाकी structured content को नहीं बदला गया। फिर edit करना हो तो ✏️ Edit Draft दबाएँ।",reply_markup=case_actions(state))
    except Exception:
        log.exception("render after edit failed"); await q.message.reply_text("❌ Change save हुआ लेकिन updated document render नहीं हो सका। /summary से draft फिर खोलें।")

async def button(update,context):
    q=update.callback_query; await q.answer()
    if not access.is_authorized(q.from_user.id):
        await q.edit_message_text("🔒 यह bot private है। आप authorized नहीं हैं। /myid से अपना User ID देखें।")
        return
    uid=q.from_user.id; cid=case_id_for(uid)
    if q.data.startswith("type:"):
        t=q.data.split(":",1)[1]; state=CaseState(cid,document_type=t); store.save(uid,state)
        await q.edit_message_text(f"⚖️ *{DISPLAY_NAMES[t]}*\n\nProgress: {progress(state)}\n\n{questions_text(state)}\n\nआप Hindi/English text या voice note भेज सकते हैं।",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="cancel")]])); return
    state=store.get(cid)
    if q.data=="cancel": store.delete(cid); await q.edit_message_text("✅ Draft session cancelled."); return
    if not state: await q.edit_message_text("कोई active draft नहीं है। /newcase से शुरू करें।"); return
    if q.data=="review": await q.edit_message_text(summary_text(state),parse_mode="Markdown",reply_markup=case_actions(state)); return
    if q.data=="edit_mode":
        state.edit_mode=True; state.pending_edit={}; store.save(uid,state)
        await q.edit_message_text("✏️ *DRAFT EDIT MODE*\n\nअब अपना बदलाव text में लिखें या voice note भेजें।\n\nउदाहरण: `पैराग्राफ 3 में 15.06.2026 की जगह 20.06.2026 कर दो।`\n\nहर instruction पर पहले preview मिलेगा, फिर आप Apply/Discard चुनेंगे।",parse_mode="Markdown"); return
    if q.data=="edit_apply": await apply_edit(update,context); return
    if q.data=="edit_discard":
        state.pending_edit={}; store.save(uid,state); await q.edit_message_text("❌ Proposed change discarded. Original draft unchanged.",reply_markup=case_actions(state)); return
    if q.data=="generate":
        if missing_fields(state.facts,state.document_type): await q.edit_message_text(questions_text(state)); return
        await generate_for(update,uid,state,edit=True)

def build_application():
    token=os.getenv("TELEGRAM_BOT_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    app=Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("newcase",newcase)); app.add_handler(CommandHandler("cancel",cancel)); app.add_handler(CommandHandler("summary",summary)); app.add_handler(CommandHandler("edit",edit_command)); app.add_handler(CommandHandler("myid",myid)); app.add_handler(CommandHandler("authorize",authorize)); app.add_handler(CommandHandler("unauthorize",unauthorize)); app.add_handler(CommandHandler("authorized",authorized))
    app.add_handler(CallbackQueryHandler(button)); app.add_handler(MessageHandler(filters.VOICE,voice_message)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_message))
    return app

def main(): build_application().run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=="__main__": main()
