"""对账工具：portal 事件流 vs 客户端决策日志。
用法:
  python tools/room_audit.py events.json        # 单局
  python tools/room_audit.py portal_events/<room>/   # 整场 10 局汇总"""
import glob
import io
import json
import os
import sys
from collections import Counter


def audit_game(path, logs_glob='logs/*.jsonl', our_user='u_a3a5624dce45'):
    data = json.load(open(path, encoding='utf-8'))
    game_id = data['game_id']
    seats = data.get('seats') or []
    our_seat = next((i for i, s in enumerate(seats) if s.get('user_id') == our_user), 0)

    server = {}
    current = 1
    for block in data['blocks']:
        for e in block.get('events') or []:
            if e['type'] == 'round_ended':
                d = e.get('data') or {}
                server.setdefault(current, []).append(
                    (e['seq'], f"胡seat{e['seat']}={d.get('detail')}fan{d.get('fan')}", e['ts']))
                current = (d.get('round_no') or current) + 1
                continue
            if e.get('seat') != our_seat:
                continue
            if e['type'] == 'tile_discarded':
                server.setdefault(current, []).append((e['seq'], e['tile'], e['ts']))
            elif e['type'] == 'tile_drawn':
                server.setdefault(current, []).append((e['seq'], f"摸{e['tile']}", e['ts']))

    client = {}
    for path_log in glob.glob(logs_glob):
        for line in io.open(path_log, encoding='utf-8', errors='replace'):
            if game_id not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            p = rec.get('payload') or {}
            if p.get('game_id') != game_id or rec.get('kind') != 'decision':
                continue
            d = p.get('decision') or {}
            rn = p.get('round_no')
            if d.get('action') == 'discard':
                client.setdefault(rn, []).append(d.get('tile'))
            elif d.get('action') == 'hu':
                client.setdefault(rn, []).append('HU')

    total_ghost = 0
    total_autohu = 0
    total_draws = 0
    per_round = []
    for rn in sorted(server):
        entries = server[rn]
        s_discards = [t for _, t, _ in entries if not t.startswith('摸') and not t.startswith('胡')]
        c_discards = [t for t in client.get(rn, []) if t != 'HU']
        c_hu = 'HU' in client.get(rn, [])
        win0 = any(t.startswith('胡seat0') for _, t, _ in entries)
        draws = sum(1 for _, t, _ in entries if t.startswith('摸'))
        total_draws += draws
        missing = Counter(s_discards) - Counter(c_discards)
        ghosts = sum(missing.values())
        total_ghost += ghosts
        flag = ''
        if win0 and not c_hu:
            flag += ' <-- 疑似托管自动胡'
            total_autohu += 1
        if ghosts:
            flag += f' <-- 疑似托管摸打 {dict(missing)}'
        per_round.append((rn, ghosts, draws, flag))
    return game_id, total_ghost, total_autohu, total_draws, per_round


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~\\Downloads\\events.json')
    if os.path.isdir(target):
        files = sorted(glob.glob(os.path.join(target, '*.json')))
    else:
        files = [target]
    grand_ghost = 0
    grand_autohu = 0
    grand_draws = 0
    for path in files:
        game_id, ghosts, autohu, draws, per_round = audit_game(path)
        detail = ' '.join(f"r{rn}:{g}/{d}" for rn, g, d, _ in per_round)
        print(f"{game_id}: 托管弃牌={ghosts} 托管自动胡={autohu} 摸牌={draws}  [{detail}]")
        grand_ghost += ghosts
        grand_autohu += autohu
        grand_draws += draws
    games = len(files)
    print(f"\n=== 汇总 {games} 局游戏: 托管弃牌 {grand_ghost} + 托管自动胡 {grand_autohu}"
          f" = {grand_ghost + grand_autohu} 轮 / 我方摸牌 {grand_draws}"
          f" = {(grand_ghost + grand_autohu) * 100.0 / max(grand_draws, 1):.1f}% ===")


if __name__ == '__main__':
    main()
