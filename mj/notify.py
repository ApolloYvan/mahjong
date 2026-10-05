"""SSE 通知读取器：只作唤醒信号，状态仍从 /state 权威读取。"""
import json


def notify_sequences(api, game_id):
    response = api.notify(game_id)
    try:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[5:].strip())
            yield payload
    finally:
        response.close()
