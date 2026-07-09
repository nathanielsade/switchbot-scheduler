import logging

from .config import load_config
from .telegram_app import build_application


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("home_agent")
    config = load_config()
    if not config.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (put it in .env).")
    if not config.allowed_chat_ids:
        log.warning("ALLOWED_CHAT_IDS is empty — DISCOVERY MODE: I will reply with each "
                    "chat's ID and will NOT run the agent until the allow-list is set.")
    log.info("home-agent starting (model=%s, db=%s)", config.model, config.db_path)
    app = build_application(config)
    app.run_polling()


if __name__ == "__main__":
    main()
