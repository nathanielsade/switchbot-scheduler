import asyncio
import logging

from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, filters

from .agent import run_turn
from .calendar_pending import CalendarPending
from .gcal import build_calendar_tools, load_calendar_service
from .home import build_home_tools, load_registry
from .memory import Conversation
from .prompts import FAMILY_SYSTEM_PROMPT
from .schedule_store import ScheduleStore
from .schedules import build_schedule_tools
from .shopping import build_shopping_tools
from .shopping_store import ShoppingStore
from .tools import DEFAULT_TOOLS

log = logging.getLogger("home_agent")

_ERROR_REPLY = "מצטער, נתקלתי בתקלה רגעית. נסו שוב עוד רגע."  # "sorry, momentary glitch, try again"

_TELEGRAM_MAX_CHARS = 4096  # a single sendMessage may not exceed this


def _split_for_telegram(text, limit=_TELEGRAM_MAX_CHARS):
    """Split a reply into Telegram-sized chunks, preferring newline boundaries.
    A single reply over 4096 chars would otherwise be rejected with BadRequest."""
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:  # no newline to split on within the window
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    chunks.append(remaining)
    return chunks


def handle_message(chat_id, text, *, config, conversation, client,
                   tools=DEFAULT_TOOLS, system=FAMILY_SYSTEM_PROMPT, model=None,
                   calendar_service=None, calendar_pending=None):
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
    turn_tools = list(tools)
    if calendar_service is not None and calendar_pending is not None:
        # Snapshot BEFORE run_turn: a change staged DURING this turn must not be committable
        # this same turn (same-turn guard) — commit_calendar_change compares against this id.
        committable_id = (calendar_pending.current(chat_id) or {}).get("id")
        turn_tools = turn_tools + build_calendar_tools(
            calendar_service, calendar_pending, chat_id, committable_id,
            calendar_ids=config.calendar_ids, write_id=config.calendar_write_id)
    history = conversation.load(chat_id)
    try:
        reply = run_turn(text, history, client=client, model=model, system=system, tools=turn_tools)
    except Exception:
        log.exception("agent error for chat_id=%s", chat_id)
        return _ERROR_REPLY
    if not reply or not reply.strip():
        # An empty completion is a soft failure: reply with the fallback rather than going
        # silent, and do NOT persist an empty assistant turn that would pollute future context.
        log.warning("agent produced an empty reply for chat_id=%s", chat_id)
        return _ERROR_REPLY
    conversation.append(chat_id, "user", text)
    conversation.append(chat_id, "assistant", reply)
    return reply


def build_application(config, *, client=None, conversation=None):
    """Build the long-poll Telegram Application. Injectable client/conversation for tests
    (no network is touched until .run_polling())."""
    if client is None:
        from openai import OpenAI
        # Cap a hung request at config.openai_timeout instead of the SDK's 600s default, so a
        # stalled OpenAI call can't freeze the (sequentially-dispatched) bot for minutes.
        client = OpenAI(api_key=config.openai_api_key, timeout=config.openai_timeout)
    if conversation is None:
        conversation = Conversation(config.db_path)
    cal_service = load_calendar_service(config)
    cal_pending = CalendarPending(config.db_path) if cal_service is not None else None
    registry = load_registry(config)
    tools = list(DEFAULT_TOOLS)
    tools += build_shopping_tools(ShoppingStore(config.db_path))
    if registry is not None:
        tools += build_home_tools(registry)
        tools += build_schedule_tools(registry, ScheduleStore(config.db_path))
    else:
        log.warning("devices file not found at %s — home control + scheduling disabled", config.devices_path)
    app = Application.builder().token(config.telegram_bot_token).build()

    async def on_message(update, context):
        message = update.effective_message
        if message is None or update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        chat = update.effective_chat

        async def _keep_typing():
            # Show "typing…" while the (multi-second) agent turn runs, so the bot never looks stuck.
            # Telegram clears the indicator after ~5s, so refresh it periodically.
            try:
                while True:
                    await chat.send_action(ChatAction.TYPING)
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass

        typing = asyncio.create_task(_keep_typing())
        try:
            reply = await asyncio.to_thread(
                handle_message, chat_id, message.text or "",
                config=config, conversation=conversation, client=client, tools=tools,
                calendar_service=cal_service, calendar_pending=cal_pending)
        finally:
            typing.cancel()
        if reply:
            for chunk in _split_for_telegram(reply):
                await message.reply_text(chunk)

    async def on_error(update, context):
        # Last-resort net so a failed send / handler error is logged, not swallowed silently.
        log.exception("unhandled error in Telegram handler", exc_info=context.error)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app
