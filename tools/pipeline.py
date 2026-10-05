"""Bot 数据管线命令。"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mj.ab import run
from mj.benchmark import compare as benchmark_compare
from mj.compare import compare
from mj.dataset import extract_file
from mj.fit import fit
from mj.gamesim import run_games
from mj.piao import estimate
from mj.replay import collect_test_room
from mj.replay_analysis import replay
from mj.report import build_report
from mj.samples import build_samples
from mj.special import run_special_cases
from mj.timing import analyze


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect"); collect.add_argument("server"); collect.add_argument("room_id", nargs="+"); collect.add_argument("--token")
    extract = sub.add_parser("extract"); extract.add_argument("--input", default="models/events/*.json")
    report = sub.add_parser("report"); report.add_argument("--room-id", nargs="*")
    timing = sub.add_parser("timing"); timing.add_argument("--logs", default="logs/*.jsonl"); timing.add_argument("--events", default="models/events/*.json"); timing.add_argument("--output", default="models/timing_report.json"); timing.add_argument("--room-id", nargs="*"); timing.add_argument("--game-prefix")
    compare_parser = sub.add_parser("compare"); compare_parser.add_argument("--input", default="models/events/*.json")
    samples_parser = sub.add_parser("samples"); samples_parser.add_argument("--logs", default="logs/*.jsonl")
    fit_parser = sub.add_parser("fit"); fit_parser.add_argument("--input", default="models/decision_samples.json")
    ab_parser = sub.add_parser("ab"); ab_parser.add_argument("--rounds", type=int, default=1000)
    replay_parser = sub.add_parser("replay"); replay_parser.add_argument("--input", default="models/events/*.json")
    gamesim_parser = sub.add_parser("gamesim"); gamesim_parser.add_argument("--rounds", type=int, default=100); gamesim_parser.add_argument("--output", default="models/game_simulation.json")
    benchmark_parser = sub.add_parser("benchmark"); benchmark_parser.add_argument("input")
    special_parser = sub.add_parser("special"); special_parser.add_argument("--output", default="models/special_cases.json")
    piao_parser = sub.add_parser("piao-rate"); piao_parser.add_argument("--logs", default="logs/*.jsonl")
    args = parser.parse_args()
    if args.command == "collect":
        for rid in args.room_id:
            print(f"采集 {rid}:", collect_test_room(args.server, rid, token=args.token), "局")
    elif args.command == "extract": print("样本数:", sum(extract_file(p, p.replace("events", "dataset")) for p in glob.glob(args.input)))
    elif args.command == "report":
        room_ids = args.room_id or [None]
        for rid in room_ids:
            print(build_report("models/events/*.json", room_id=rid))
    elif args.command == "timing":
        room_ids = args.room_id or [None]
        for rid in room_ids:
            print(analyze(args.logs, args.events, args.output, room_id=rid, game_prefix=args.game_prefix))
    elif args.command == "compare": print(compare(args.input))
    elif args.command == "samples":
        samples = build_samples(args.logs); print({"samples": len(samples), "baseline": sum(s["baseline_match"] for s in samples), "route": sum(s["route_match"] for s in samples)})
    elif args.command == "fit": print(fit(args.input))
    elif args.command == "ab": print(run(rounds=args.rounds))
    elif args.command == "replay": print(replay(args.input))
    elif args.command == "gamesim": print(run_games(rounds=args.rounds, output=args.output))
    elif args.command == "benchmark": print(benchmark_compare(args.input))
    elif args.command == "special": print(run_special_cases(args.output))
    elif args.command == "piao-rate": print(estimate(args.logs))


if __name__ == "__main__": main()
