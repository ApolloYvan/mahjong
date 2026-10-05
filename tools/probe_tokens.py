import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.api import MahjongApi
from mj.security import token_fingerprint

SERVER = os.environ.get("MJ_SERVER", "https://10.240.169.190:18080")

TOKEN_ENV_NAMES = ["MJ_TOKEN_1", "MJ_TOKEN_2", "MJ_TOKEN_3", "MJ_TOKEN_4"]
tokens = {name: os.environ.get(name) for name in TOKEN_ENV_NAMES}
tokens = {name: token for name, token in tokens.items() if token}

if not tokens:
    sys.exit(
        "missing required environment variables: at least one of "
        + ", ".join(TOKEN_ENV_NAMES)
    )

for name, token in tokens.items():
    api = MahjongApi(SERVER, token)
    fingerprint = token_fingerprint(token)
    try:
        me = api.me()
        print(name, "fingerprint=", fingerprint, "OK user=", me.get("user_id"),
              "room=", me.get("tournament_id"), "active=", len(me.get("active_games") or []))
    except Exception as error:
        print(name, "fingerprint=", fingerprint, "FAIL", repr(error)[:100])
