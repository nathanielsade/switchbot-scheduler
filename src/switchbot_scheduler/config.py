"""Load a local, git-ignored .env into the environment.

Convenience so `switchbot-ui` / `switchbot-schedule` pick up OPENAI_API_KEY
without passing it inline every launch. Never overrides variables already set
in the shell (an explicit `export` wins), and the .env file is git-ignored.
"""
from dotenv import load_dotenv


def load_env(path: str | None = None) -> None:
    load_dotenv(dotenv_path=path, override=False)
