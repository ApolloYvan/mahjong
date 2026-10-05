import json
import os
import ssl
import time
import urllib.request

from .api import MahjongApi

_CONTEXT = ssl._create_unverified_context()


def _get_json(url, token=None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=35, context=_CONTEXT) as response:
        return json.loads(response.read().decode("utf-8"))


def collect_test_room(server, room_id, output_dir="models/events", wait=2, token=None):
    os.makedirs(output_dir, exist_ok=True)
    batches = set()
    games_url = f"{server.rstrip('/')}/api/test-rooms/{room_id}/games"
    games_data = _get_json(games_url, token)
    games = games_data.get("games", games_data)
    for game in games:
        batch = str(game["batch"])
        if game.get("status") != "finished":
            continue
        path = os.path.join(output_dir, f"{game['game_id']}.json")
        if os.path.exists(path):
            batches.add(batch)
            continue
        events = _get_json(f"{games_url}/{batch}/events", token)
        time.sleep(0.25)
        with open(path, "w", encoding="utf-8") as output:
            json.dump(events, output, ensure_ascii=False, indent=2)
        batches.add(batch)
    return len(batches)
