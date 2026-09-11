"""Telegram entry point for the Legal Drafting Bot.

Current milestone:
- /start, /newcase, /cancel
- persistent SQLite case state
- conservative labelled-fact intake
- missing-information questions
- Dava retrieval/composition
- Azure Structured Outputs drafting

Voice/OCR/final DOCX generation are intentionally separate later milestones.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.case_store import CaseStore
from app.drafting_engine.conversation_state import CaseState
from app.drafting_engine.dava_orchestrator import DavaOrchestrator
from app.drafting_engine.renderers.legal_document_renderer import render_both


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
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


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    await update.message.reply_text(
        "नमस्ते। यह Legal Drafting Bot का test version है।\n\n"
        "/newcase — नया वाद शुरू करें\n"
        "/cancel — वर्तमान case रद्द करें\n\n"
        "अभी text-based Dava/Plaint workflow सक्रिय है।\n"
        "कृपया जानकारी labelled रूप में भेजें, जैसे "
        "न्यायालय:, वादी:, प्रतिवादी:, तथ्य:, राहत:।"
    )


async def newcase(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    case_id = case_id_for(update.effective_user.id)
    state = CaseState(case_id)
    store.save(update.effective_user.id, state)

    await update.message.reply_text(
        "नया वाद शुरू हो गया है।\n\n"
        "आप मामले की जानकारी एक या कई messages में भेज सकते हैं।\n"
        "उदाहरण:\n"
        "न्यायालय: ...\n"
        "वादी: ...\n"
        "प्रतिवादी: ...\n"
        "तथ्य: ...\n"
        "राहत: ..."
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    store.delete(case_id_for(update.effective_user.id))
    await update.message.reply_text(
        "वर्तमान case रद्द कर दिया गया है। /newcase से नया case शुरू करें।"
    )


def _clean_value(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_simple_fact_input(text: str) -> dict[str, Any]:
    """Conservative labelled-field parser.

    It recognizes only explicit labels and never guesses unlabelled facts.
    Multiple labelled lines can be supplied in one Telegram message.
    """

    label_map = {
        "court_name": [
            "court",
            "न्यायालय",
        ],
        "plaintiffs": [
            "plaintiff",
            "वादी",
        ],
        "defendants": [
            "defendant",
            "प्रतिवादी",
        ],
        "reliefs": [
            "relief",
            "राहत",
            "प्रार्थना",
        ],
        "plaintiff_intro": [
            "plaintiff intro",
            "वादी परिचय",
        ],
        "valuation": [
            "valuation",
            "मूल्यांकन",
        ],
        "court_fee": [
            "court fee",
            "न्यायालय शुल्क",
        ],
        "cause_of_action": [
            "cause of action",
            "वाद-कारण",
            "वाद कारण",
        ],
        "jurisdiction_facts": [
            "jurisdiction",
            "क्षेत्राधिकार",
        ],
        "facts": [
            "facts",
            "तथ्य",
        ],
    }

    # Longest labels first prevents "plaintiff" from consuming "plaintiff intro".
    label_patterns: list[tuple[str, str]] = []
    for key, labels in label_map.items():
        for label in labels:
            label_patterns.append((key, label))
    label_patterns.sort(key=lambda x: len(x[1]), reverse=True)

    pattern = re.compile(
        r"^\s*(?P<label>"
        + "|".join(re.escape(label) for _, label in label_patterns)
        + r")\s*[:\-]\s*(?P<value>.*?)\s*$",
        flags=re.IGNORECASE,
    )

    out: dict[str, Any] = {}

    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue

        raw_label = match.group("label").strip().lower()
        value = _clean_value(match.group("value"))
        if not value:
            continue

        key = next(
            key
            for key, label in label_patterns
            if label.lower() == raw_label
        )

        if key in {"plaintiffs", "defendants", "reliefs"}:
            # Preserve explicit list entries; split only on semicolon/comma.
            parts = [
                _clean_value(x)
                for x in re.split(r"[;,]", value)
                if _clean_value(x)
            ]
            out[key] = parts or [value]
        elif key == "facts":
            existing = out.setdefault("facts", [])
            number = len(existing) + 1
            existing.append({"number": number, "text": value})
        else:
            out[key] = value

    return out


async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if (
        not update.message
        or not update.message.text
        or not update.effective_user
    ):
        return

    state = get_or_create_state(update.effective_user.id)
    text = update.message.text.strip()
    state.record("user", text)

    parsed = parse_simple_fact_input(text)
    if not parsed:
        await update.message.reply_text(
            "मैं अभी केवल स्पष्ट labelled जानकारी पढ़ रहा हूँ।\n\n"
            "कृपया इस तरह भेजें:\n"
            "न्यायालय: ...\n"
            "वादी: ...\n"
            "प्रतिवादी: ...\n"
            "तथ्य: ...\n"
            "राहत: ..."
        )
        return

    result = orchestrator.collect(state, parsed)
    store.save(update.effective_user.id, state)

    if result["status"] == "needs_information":
        questions = "\n".join(result["questions"])
        await update.message.reply_text(
            "जानकारी अभी पूरी नहीं है। कृपया इन बातों का उत्तर दें:\n\n"
            + questions
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Draft तैयार करें",
                    callback_data="draft",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="cancel",
                )
            ],
        ]
    )

    await update.message.reply_text(
        "आवश्यक न्यूनतम जानकारी मिल गई है। "
        "Draft बनाने से पहले कृपया तथ्यों की जाँच करें।",
        reply_markup=keyboard,
    )


async def button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
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
        await query.edit_message_text(
            "कोई active case नहीं है। /newcase से शुरू करें।"
        )
        return

    await query.edit_message_text("Draft तैयार किया जा रहा है…")

    try:
        draft = await asyncio.to_thread(
            orchestrator.draft_live,
            state,
        )

        output_dir = (
            __import__("pathlib").Path("/tmp/legal_drafts")
            / str(query.from_user.id)
        )
        docx_path, pdf_path = await asyncio.to_thread(
            render_both,
            draft,
            output_dir,
            f"Dava_Draft_{state.case_id}",
            "letter",
        )

        await query.message.reply_text(
            "✅ Dava draft तैयार है।\n\n"
            "नीचे editable DOCX और print/share-ready PDF दोनों भेज रहा हूँ।"
        )

        with docx_path.open("rb") as docx_file:
            await query.message.reply_document(
                document=docx_file,
                filename=docx_path.name,
                caption="📄 DOCX — editable draft",
            )

        with pdf_path.open("rb") as pdf_file:
            await query.message.reply_document(
                document=pdf_file,
                filename=pdf_path.name,
                caption="📕 PDF — print/share copy",
            )

        state.status = "drafted"
        store.save(query.from_user.id, state)

    except Exception as exc:
        log.exception("Draft failed")
        await query.message.reply_text(
            "Draft अभी generate नहीं हो सका। "
            "Azure configuration और deployment जाँचें.\n"
            f"Technical detail: {type(exc).__name__}"
        )


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("newcase", newcase))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message,
        )
    )

    return application


def main() -> None:
    application = build_application()
    log.info("Legal Drafting Bot starting.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
