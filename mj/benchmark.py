"""自对弈结果统计与两比例 Wilson 置信区间。"""
import json
import math


def wilson(successes, total, z=1.96):
    if not total:
        return [0.0, 0.0]
    p = successes / total
    den = 1 + z * z / total
    center = (p + z * z / (2 * total)) / den
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / den
    return [center - radius, center + radius]


def load(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def compare(path):
    report = load(path)
    rounds = report["rounds"]
    stats = report["stats"]
    result = {"rounds": rounds}
    for name in ("baseline", "route"):
        wins = stats[f"{name}_wins"]
        result[name] = {
            "wins": wins,
            "win_rate": wins / rounds,
            "win_ci95": wilson(wins, rounds),
            "avg_draws": stats[f"{name}_draw_total"] / rounds,
            "avg_fan": stats[f"{name}_fan_total"] / rounds,
        }
    result["win_rate_difference"] = result["route"]["win_rate"] - result["baseline"]["win_rate"]
    return result
