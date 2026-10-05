import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.api import MahjongApi
from mj.security import token_fingerprint

TOKEN = os.environ.get("MJ_TOKEN")
if not TOKEN:
    sys.exit("missing required environment variable: MJ_TOKEN")

SERVER = os.environ.get("MJ_SERVER", "https://10.240.169.190:18080")

print("using token fingerprint:", token_fingerprint(TOKEN))

api = MahjongApi(SERVER, TOKEN, pace_interval=0)

me = api.me()
games = me.get("active_games") or []
print("active_games:", len(games))
if not games:
    print("no active game to probe")
    raise SystemExit(0)

game_id = games[0] if isinstance(games[0], str) else games[0].get("game_id")
print("probing", game_id)

latencies = []
for i in range(8):
    started = time.monotonic()
    api.state(game_id, 0, timeout=5.0)
    elapsed = (time.monotonic() - started) * 1000
    latencies.append(elapsed)
    print(f"  probe {i + 1}: {elapsed:.0f}ms")
    time.sleep(1.0)

fast = sum(1 for v in latencies if v <= 125)
print(f"fast(<=125ms): {fast}/8")
