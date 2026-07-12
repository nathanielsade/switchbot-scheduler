"""One-time Roborock login → saves the auth token for the bot to reuse.

Emails a verification code to ROBOROCK_USERNAME, prompts for it, then writes the account token to
ROBOROCK_USERDATA (default roborock_userdata.json, git-ignored). Works for accounts created via the
app / Google sign-in that have no password. Run:  python scripts/roborock_login.py

Both steps run in ONE process on purpose: the emailed code is tied to the client's header id
(md5(username + a per-client random device id)), so requesting and redeeming the code must share the
same RoborockApiClient instance — doing them in separate runs yields "invalid code"."""
import asyncio
import json
import sys

from home_agent.config import load_config
from roborock.web_api import RoborockApiClient


async def main() -> int:
    cfg = load_config()
    if not cfg.roborock_username:
        print("Set ROBOROCK_USERNAME (your Roborock account email) first.", file=sys.stderr)
        return 1
    api = RoborockApiClient(cfg.roborock_username)
    await api.request_code()
    code = input(f"Enter the verification code emailed to {cfg.roborock_username}: ").strip()
    user_data = await api.code_login(code)
    with open(cfg.roborock_userdata_path, "w") as f:
        json.dump(user_data.as_dict(), f)
    print(f"Saved auth token to {cfg.roborock_userdata_path} (git-ignored). The bot will reuse it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
