import os
import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from app.case_store import CaseStore
from app.drafting_engine.case_intake import CaseIntakeEngine
from app.drafting_engine.dava_structure_planner import DavaStructurePlanner
from app.drafting_engine.dava_composer import DavaComposer
from app.drafting_engine.dava_validator import DavaValidator
from app.drafting_engine.renderers.legal_document_renderer import LegalDocumentRenderer
from app.drafting_engine.dava_orchestrator import DavaOrchestrator
from app.drafting_engine.azure_client import AzureClient
from app.drafting_engine.retriever import Retriever

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class LegalBot:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        
        # Initialize pipeline dependencies
        self.state_store = CaseStore(db_path="cases.db")
        azure_client = AzureClient()
        retriever = Retriever()
        
        intake_engine = CaseIntakeEngine(azure_client)
        planner = DavaStructurePlanner(azure_client)
        composer = DavaComposer(azure_client, retriever)
        validator = DavaValidator()
        renderer = LegalDocumentRenderer()
        
        self.orchestrator = DavaOrchestrator(
            state_store=self.state_store,
            intake_engine=intake_engine,
            planner=planner,
            composer=composer,
            validator=validator,
            renderer=renderer
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_msg = (
            "नमस्ते। मैं वी०डी० शुक्ला एडवोकेट का लीगल ड्राफ्टिंग असिस्टेंट हूँ।\n\n"
            "मैं आपके मामले के तथ्यों को समझकर कोर्ट के लिए वाद पत्र (Dava) तैयार कर सकता हूँ।\n"
            "नया मामला शुरू करने के लिए /newcase का उपयोग करें।"
        )
        await update.message.reply_text(welcome_msg)

    async def newcase(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        self.state_store.clear_state(user_id)  # Safely wipe old case
        self.state_store.save_state(user_id, {"status": "intake", "facts": []})
        await update.message.reply_text("नया मामला शुरू किया गया है। कृपया मुझे घटना के तथ्य बताएं (जैसे: वादी कौन है, संपत्ति क्या है, क्या हुआ?)")

    async def summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        state = self.state_store.get_state(user_id)
        
        if not state or not state.get("facts"):
            await update.message.reply_text("अभी तक कोई तथ्य दर्ज नहीं किया गया है।")
            return
            
        summary_msg = "**वर्तमान मामले का विवरण:**\n\n"
        summary_msg += f"**वादी:** {state.get('plaintiffs', 'अज्ञात')}\n"
        summary_msg += f"**प्रतिवादी:** {state.get('defendants', 'अज्ञात')}\n"
        summary_msg += "**तथ्य:**\n" + "\n".join([f"- {f}" for f in state.get('facts', [])])
        
        await update.message.reply_text(summary_msg, parse_mode='Markdown')

    async def redraft(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        state = self.state_store.get_state(user_id)
        if not state:
            await update.message.reply_text("कोई सक्रिय मामला नहीं है। /newcase का प्रयोग करें।")
            return
            
        state['status'] = 'correction'
        self.state_store.save_state(user_id, state)
        await update.message.reply_text("ड्राफ्ट रद्द कर दिया गया है। कृपया बताएं कि क्या सुधार करना है।")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        user_text = update.message.text
        
        # Route through the restored orchestrator method
        if user_text == "✅ Dava Draft तैयार करें":
            await update.message.reply_text("ड्राफ्ट तैयार किया जा रहा है, कृपया प्रतीक्षा करें...")
            # Generating both DOCX and PDF deterministically
            docx_path, pdf_path = self.orchestrator.execute_drafting_pipeline(user_id)
            
            if docx_path and os.path.exists(docx_path):
                await update.message.reply_document(document=open(docx_path, 'rb'))
            if pdf_path and os.path.exists(pdf_path):
                await update.message.reply_document(document=open(pdf_path, 'rb'))
        else:
            bot_reply = self.orchestrator.extract_and_collect(user_id, user_text)
            
            # Simple reply keyboard for progressing to draft
            keyboard = [["✅ Dava Draft तैयार करें"]]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
            await update.message.reply_text(bot_reply, reply_markup=reply_markup)

    def run(self):
        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("newcase", self.newcase))
        app.add_handler(CommandHandler("summary", self.summary))
        app.add_handler(CommandHandler("redraft", self.redraft))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        app.run_polling()

if __name__ == "__main__":
    bot = LegalBot()
    bot.run()
