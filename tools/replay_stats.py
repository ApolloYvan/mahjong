"""r3 复盘分析：胡牌细节、庄家得分占比、超时与 409 统计。"""
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOM = sys.argv[1] if len(sys.argv) > 1 else "t_e948daaa1916"
ROUND = sys.argv[2] if len(sys.argv) > 2 else "r1"

events_dir = "models/events"
discard_timeout = Counter()
wins_by_seat = Counter()
win_scores = []
dealer_score = Counter()
dealer_wins = 0
rounds = 0
draws = 0
discards = 0

for name in sorted(os.listdir(events_dir)):
    if not name.startswith(f"{ROOM}_{ROUND}_"):
        continue
    with open(os.path.join(events_dir, name), encoding="utf-8") as source:
        data = json.load(source)
    game_id = data.get("game_id", name)
    rounds_data = data.get("rounds") or []
    blocks = data.get("blocks", [])
    for rd in rounds_data:
        rounds += 1
        winner = rd.get("winner")
        scores = rd.get("scores") or []
        dealer = rd.get("dealer")
        if rd.get("is_draw"):
            draws += 1
        elif winner is not None and 0 <= winner < len(scores):
            wins_by_seat[winner] += 1
            win_scores.append((game_id, winner, dealer, rd.get("multiplier"), scores))
            if winner == dealer:
                dealer_wins += 1
        if dealer is not None and 0 <= dealer < len(scores):
            dealer_score[dealer] += scores[dealer]
    for block in blocks:
        for event in block.get("events", []):
            etype = event.get("type")
            if etype == "timeout":
                kind = (event.get("data") or {}).get("kind")
                if kind == "discard":
                    discard_timeout[event.get("seat")] += 1
            elif etype == "tile_discarded":
                discards += 1

print(f"=== {ROOM} {ROUND} 复盘 ===")
print(f"局数: {rounds}  胡牌: {rounds - draws}  流局: {draws}")
print(f"各座位胡牌: {dict(wins_by_seat)}  其中自摸庄家胡: {dealer_wins}")
if win_scores:
    mults = [m for _, _, _, m, _ in win_scores]
    print(f"倍数(=番x链)分布: {sorted(mults)}")
print(f"出牌超时(按座位): {dict(discard_timeout)}  合计 {sum(discard_timeout.values())}")
print(f"做庄得分合计(按庄家座位): {dict(dealer_score)}")
print()
print("胡牌明细 (局, 胡家, 是否庄, 倍数, 得分):")
for game_id, winner, dealer, mult, scores in win_scores:
    tag = "庄" if winner == dealer else "闲"
    print(f"  {game_id.split(ROOM + '_')[1]}  座位{winner}({tag})  x{mult}  {scores}")

decision_kinds = Counter()
hu_submits = 0
logs_dir = "logs"
if os.path.isdir(logs_dir):
    for log_name in sorted(os.listdir(logs_dir)):
        if not log_name.endswith(".jsonl"):
            continue
        path = os.path.join(logs_dir, log_name)
        with open(path, encoding="utf-8", errors="ignore") as source:
            for line in source:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("kind") != "decision":
                    continue
                if not (item.get("payload") or {}).get("game_id", "").startswith(f"{ROOM}_{ROUND}_"):
                    continue
                action = (item.get("payload") or {}).get("decision", {}).get("action")
                decision_kinds[action] += 1
                if action == "hu":
                    hu_submits += 1

print()
print(f"Bot 决策分布: {dict(decision_kinds)}  主动hu提交: {hu_submits}")
