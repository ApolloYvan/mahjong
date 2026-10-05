"""s54-b0-r2 逐张弃牌考量重放: 用决策日志里的完整手牌重跑评分器,
输出每次弃牌的候选排名 / 与次优差距 / 备选方案。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.strategy import _discard_score
from mj.fit import load_weights

GAME = 'a_a379893d6fbb_r1_b0_t0'
w = load_weights()

records = []
claims = []
round_end = None
for line in io.open('logs/2026-09-20.jsonl', encoding='utf-8', errors='replace'):
    if GAME not in line:
        continue
    try:
        rec = json.loads(line)
    except Exception:
        continue
    p = rec.get('payload') or {}
    if p.get('game_id') != GAME:
        continue
    kind = rec.get('kind')
    d = p.get('decision') or {}
    if kind == 'decision' and d.get('action') == 'discard':
        records.append(p)
    elif kind == 'decision' and d.get('action') in ('chi', 'peng', 'gang'):
        claims.append((p.get('round_no'), d.get('action'), d.get('tile'),
                       (d.get('tiles') or [])))
    elif kind == 'result':
        round_end = p

r2 = [p for p in records if p.get('round_no') == 2]
print(f'第2局弃牌决策 {len(r2)} 次, 吃碰杠 {len([c for c in claims if c[0] == 2])} 次')

for i, p in enumerate(r2):
    hand = p.get('hand') or []
    melds = p.get('melds') or [[], [], [], []]
    seat = p.get('seat', -1)
    meld_groups = len(melds[seat]) if isinstance(melds, list) and seat < len(melds) else 0
    god = p.get('god') or {}
    chain = god.get('chain_count', 0)
    piao = god.get('piao', p.get('piao', 0))
    drawn = p.get('drawn_tile')
    chosen = (p.get('decision') or {}).get('tile')
    scores = {}
    for tile in sorted(set(hand)):
        scores[tile] = _discard_score(hand, tile, meld_groups, chain, piao, None, w, None)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    rank = [t for t, _ in ranked].index(chosen) + 1 if chosen in scores else -1
    margin = (ranked[0][1] - scores.get(chosen, 0)) if chosen in scores else 0
    print(f"\n#{i + 1} 墙={p.get('wall_remaining')} 摸 {drawn} → 弃 {chosen}  "
          f"(评分第 {rank}, 与榜首差 {margin:.0f})")
    print(f"    手: {' '.join(hand)}")
    print("    top3: " + "  ".join(f"{t}:{v:.0f}" for t, v in ranked[:3]))
    jokers = hand.count('白')
    if jokers:
        print(f"    白×{jokers} 在手 (joker 计划语境: 向听搜索按百搭最优使用)")
