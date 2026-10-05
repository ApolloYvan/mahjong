import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.api import MahjongApi
from mj.security import token_fingerprint

TOKEN = os.environ.get("MJ_TOKEN")
if not TOKEN:
    sys.exit("missing required environment variable: MJ_TOKEN")

SERVER = os.environ.get("MJ_SERVER", "https://10.240.169.190:18080")
TOURNAMENT_ID = os.environ.get("MJ_TOURNAMENT_ID", "a_d84d9fcdecc4")

print("using token fingerprint:", token_fingerprint(TOKEN))

api = MahjongApi(SERVER, TOKEN)
try:
    info = api.tournament(TOURNAMENT_ID)
    print(json.dumps({k: info.get(k) for k in ("status", "voided_reason", "registered_users", "ready_users", "config")}, ensure_ascii=False, indent=1))
    print("my_games:", len(info.get("my_games") or []))
    print("ranking:", json.dumps(info.get("ranking"), ensure_ascii=False)[:400])
except Exception as error:
    print("room query fail:", repr(error))
