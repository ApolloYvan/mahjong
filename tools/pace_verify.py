import io
import json
import sys

window_start = sys.argv[1] if len(sys.argv) > 1 else '2026-09-20T05:10'  # 13:10 本地 = 新验证会话起点
s44_window = ('2026-09-20T01:05', '2026-09-20T03:40')

records = {'state': [], 'error': [], 'decision': 0, 'hu_detail': 0}
s44_states = []
for line in io.open('logs/2026-09-20.jsonl', encoding='utf-8', errors='replace'):
    if '"state"' not in line and '"error"' not in line and '"decision"' not in line and '"hu_detail"' not in line:
        continue
    try:
        rec = json.loads(line)
    except Exception:
        continue
    t = rec.get('time', '')
    kind = rec.get('kind')
    p = rec.get('payload') or {}
    if s44_window[0] <= t[:16] <= s44_window[1]:
        if kind == 'state':
            s44_states.append(p.get('state_request_ms') or 0)
        elif kind == 'error' and '429' in str(p.get('error', '')):
            records.setdefault('s44_429', 0)
            records['s44_429'] += 1
    if t[:16] >= window_start:
        if kind == 'state':
            records['state'].append(p.get('state_request_ms') or 0)
        elif kind == 'error':
            records['error'].append(str(p.get('error'))[:80])
        elif kind == 'decision':
            records['decision'] += 1
        elif kind == 'hu_detail':
            records['hu_detail'] += 1

def dist(values):
    if not values:
        return 'n/a'
    values = sorted(values)
    n = len(values)
    buckets = {'<=125ms': 0, '126-400': 0, '401-900': 0, '>900': 0}
    for v in values:
        if v <= 125:
            buckets['<=125ms'] += 1
        elif v <= 400:
            buckets['126-400'] += 1
        elif v <= 900:
            buckets['401-900'] += 1
        else:
            buckets['>900'] += 1
    pct = {k: f"{v * 100 // n}%" for k, v in buckets.items()}
    return (f"n={n} p50={values[n // 2]:.0f}ms p95={values[int(n * 0.95)]:.0f}ms "
            f"max={values[-1]:.0f}ms {pct}")

print('s44 基线 (01:05-03:40 UTC):')
print('  state', dist(s44_states))
print('  429 errors:', records.get('s44_429', 0))
print()
print('新会话 (pacer 后, 05:10 UTC 起):')
print('  state', dist(records['state']))
print('  errors:', len(records['error']), records['error'][:5])
print('  decisions:', records['decision'], 'hu_detail:', records['hu_detail'])
