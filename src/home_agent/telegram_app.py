import logging

from .agent import run_turn
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
