import asyncio
import logging

from telegram.ext import Application, MessageHandler, filters

from .agent import run_turn
from .memory import Conversation
from .prompts import FAMILY_SYSTEM_PROMPT
from .tools import DEFAULT_TOOLS

log = logging.getLogger("home_agent")

_ERROR_REPLY = "מצטער, נתקלתי בתקלה רגעית. נסו שוב עוד רגע."  # "sorry, momentary glitch, try again"


def handle_message(chat_id, text, *, config, conversation, client,
                   tools=DEFAULT_TOOLS, system=FAMILY_SYSTEM_PROMPT, model=None):
    """Process one inbound Telegram text message. Returns the reply text, or None to stay silent.
    Sequencing note: history is loaded BEFORE the current message is persisted, so the current
    turn is not duplicated in the model context."""
    model = model or config.model
    log.info("incoming chat_id=%s text_len=%d", chat_id, len(text or ""))
    if not config.allowed_chat_ids:  # discovery mode: allow-list not configured yet
        log.warning("discovery mode (empty allow-list): revealing chat_id=%s", chat_id)
        return (f"\U0001F44B chat_id = {chat_id}\n"
                "Add it to ALLOWED_CHAT_IDS in .env and restart me to activate.")
    if chat_id not in config.allowed_chat_ids:
        log.warning("ignoring message from unauthorized chat_id=%s", chat_id)
        return None
    history = conversation.load(chat_id)
    try:
        reply = run_turn(text, history, client=client, model=model, system=system, tools=tools)
    except Exception:
        log.exception("agent error for chat_id=%s", chat_id)
        return _ERROR_REPLY
    conversation.append(chat_id, "user", text)
    conversation.append(chat_id, "assistant", reply)
    return reply


def build_application(config, *, client=None, conversation=None):
    """Build the long-poll Telegram Application. Injectable client/conversation for tests
    (no network is touched until .run_polling())."""
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=config.openai_api_key)
    if conversation is None:
        conversation = Conversation(config.db_path)
    app = Application.builder().token(config.telegram_bot_token).build()

    async def on_message(update, context):
        message = update.effective_message
        if message is None or update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        reply = await asyncio.to_thread(
            handle_message, chat_id, message.text or "",
            config=config, conversation=conversation, client=client)
        if reply:
            await message.reply_text(reply)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app
