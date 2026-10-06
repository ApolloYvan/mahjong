#!/bin/sh
# 财神→爆头方向的候选筛选（2026-10-06，破釜沉舟轮）。候选在 tools/overlays/joker/*.json。
#   bash tools/screen_joker.sh screen                 # 8 个候选 × 300 种子（种子 20000 起），约 2 小时
#   bash tools/screen_joker.sh confirm J1_bt_after J5_combo   # 入围者 × 600 个全新种子（50000 起），每个约 30 分钟
#   bash tools/screen_joker.sh summary                # 只出汇总表
# 事先定好的采用规则：confirm 的全新种子上 A−B 均值 > 0 且 95% 区间下界 > −0.05，
# 并且 screen+confirm 合并后的区间下界 > 0，胜率下降不超过 1 个百分点（历史教训：调高爆头权重曾让实战胜率从 28.4% 掉到 24.1%）。
set -u
cd "$(dirname "$0")/.."
if pgrep -f "mj[.]bot" >/dev/null 2>&1; then
    echo "!!! bot 正在跑：对战平台会和 bot 抢 CPU，先停 bot"; exit 1
fi
mkdir -p reports/joker
MODE=${1:-summary}
[ $# -gt 0 ] && shift

run() {   # run 前缀 名字 种子数 起始种子 分钟上限
    f=tools/overlays/joker/$2.json
    [ -f "$f" ] || { echo "没有 $f"; return; }
    echo "=== [$(date +%H:%M)] $1 $2（$3 种子，起始 $4）==="
    caffeinate -i python3 tools/arena2.py ab --a "$f" --matches "$3" --seed "$4" --jobs "${MJ_JOBS:-6}" \
        --layout 1v3 --max-minutes "$5" --tag "$1_$2" > "reports/joker/$1_$2.log" 2>&1
    grep -E "配对差|第一名率|^  庄 A−B|^  闲 A−B" "reports/joker/$1_$2.log" | head -4
}

if [ "$MODE" = "screen" ]; then
    for f in tools/overlays/joker/*.json; do
        run scr "$(basename "$f" .json)" 300 20000 20
    done
elif [ "$MODE" = "confirm" ]; then
    for name in "$@"; do
        run cf "$name" 600 50000 40
    done
fi

python3 - <<'PY'
import glob, json, os
rows = {}
for p in glob.glob("reports/suite/scr_*_1v3.json") + glob.glob("reports/suite/cf_*_1v3.json"):
    d = json.load(open(p, encoding="utf-8"))
    stage, name = d["tag"].split("_", 1)
    rows.setdefault(name, {})[stage] = d
print("\n%-14s | %-30s | %-30s | %s" % ("候选", "筛选（300 种子）A−B [95%CI]", "确认（600 新种子）A−B [95%CI]", "庄 / 闲 A−B（筛选）"))
for name in sorted(rows, key=lambda n: -rows[n].get("scr", {}).get("mean", -9)):
    r = rows[name]
    def cell(s):
        d = r.get(s)
        return "%+.3f [%+.3f, %+.3f] n=%d" % (d["mean"], d["lo"], d["hi"], d["n_seeds"]) if d else "-"
    s = r.get("scr") or {}
    def rd(v):   # _role_diff = [均值, 标准误, 95%半宽]
        return "%+.2f±%.2f" % (v[0], v[2]) if isinstance(v, list) and len(v) == 3 else "-"
    role = "%s / %s" % (rd(s.get("dealer")), rd(s.get("non_dealer")))
    print("%-14s | %-30s | %-30s | %s" % (name, cell("scr"), cell("cf"), role))
print("\n入围：筛选均值 > +0.10 的候选进 confirm；采用规则见脚本开头。胜率看 reports/joker/<阶段>_<名字>.log 里的 [A]/[B] 胜率行。")
PY
