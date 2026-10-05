"""拉取一场会话全部 10 局的 portal 事件流。
用法: python tools/pull_all_events.py <room_id>
Cookie 从 portal_cookie.txt 读取（mahjong_sid=...），输出到 portal_events/<room>/。"""
import http.client
import json
import os
import ssl
import sys
import time

room = sys.argv[1] if len(sys.argv) > 1 else ''
if not room:
    raise SystemExit('usage: pull_all_events.py <room_id>')

cookie_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'portal_cookie.txt')
cookie = ''
for line in open(cookie_file, encoding='utf-8'):
    if line.startswith('majiang_sid='):
        cookie = line.strip().split('=', 1)[1]
if not cookie:
    raise SystemExit('portal_cookie.txt 缺少 majiang_sid')

context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE

out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'portal_events', room)
os.makedirs(out_dir, exist_ok=True)

ok = 0
for board in range(10):
    game_id = f'{room}_r1_b{board}_t0'
    out_path = os.path.join(out_dir, f'b{board}.json')
    if os.path.exists(out_path):
        print(f'b{board}: 已存在，跳过')
        ok += 1
        continue
    try:
        conn = http.client.HTTPSConnection('10.240.169.190', 18080, timeout=20, context=context)
        conn.request('GET', f'/portal/api/games/{game_id}/events', headers={
            'Cookie': f'majiang_sid={cookie}',
            'Accept': '*/*',
            'Referer': 'https://10.240.169.190:18080/portal/',
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) '
                          'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1',
        })
        response = conn.getresponse()
        body = response.read().decode('utf-8')
        if response.status != 200:
            print(f'b{board}: HTTP {response.status} {body[:120]}')
            conn.close()
            time.sleep(1)
            continue
        data = json.loads(body)
        with open(out_path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
        blocks = len(data.get('blocks') or [])
        print(f'b{board}: OK blocks={blocks}')
        ok += 1
        conn.close()
    except Exception as error:
        print(f'b{board}: FAIL {type(error).__name__} {str(error)[:120]}')
    time.sleep(1)

print(f'完成 {ok}/10 -> {out_dir}')
