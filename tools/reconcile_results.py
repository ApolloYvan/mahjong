"""服务端结算对账 CLI：比对本地 hu_detail 日志与门户事件流 round_ended 记录，
自动发现"服务端 fan4、本地 fan2"一类差异。

用法：
    python tools/reconcile_results.py --logs "logs/*.jsonl" --events "portal_events/*/*.json"

不发起任何网络请求，只读取已经落盘的 JSONL 决策日志与门户事件流 JSON 文件。
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.reconcile import (
    local_estimates_from_hu_detail_lines,
    reconcile,
    server_truths_from_portal_game,
    summarize,
)


def load_hu_detail_records(pattern):
    records = []
    for path in glob.glob(pattern):
        with open(path, encoding="utf-8", errors="ignore") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def load_server_truths(pattern):
    merged = {}
    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as source:
                data = json.load(source)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        merged.update(server_truths_from_portal_game(data))
    return merged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", default="logs/*.jsonl", help="决策日志 JSONL glob")
    parser.add_argument("--events", default="portal_events/*/*.json", help="门户事件流 JSON glob")
    args = parser.parse_args()

    local = local_estimates_from_hu_detail_lines(load_hu_detail_records(args.logs))
    server = load_server_truths(args.events)
    report = reconcile(local, server)
    print(summarize(report))
    if report["discrepancies"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
