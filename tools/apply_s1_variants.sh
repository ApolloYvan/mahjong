#!/bin/bash
# 装上 S1 放宽变体 E1/E2/E3（开关默认全关，默认值时行为不变）：新文件拷进来 + 给 4 个已有文件打补丁，然后跑相关单元测试。
# 因为这些文件（mj/hu_strategy.py、tools/arena2.py、tools/arena_suite.sh……）正在被你的批量使用时不能改，所以单独做成"批量结束后一键装上"：
#   bash tools/apply_s1_variants.sh && MJ_JOBS=6 bash tools/arena_suite.sh E1 E2 E3
# 有 arena_suite / round_batch / arena2 / mj.bot 在跑就拒绝执行；已经装过就跳过；修改前的原文件备份在 tools/.cache/s1_variants_backup/。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if pgrep -f "mj.bot|arena_suite|round_batch|arena2.py|s3_batch|s2_batch" >/dev/null; then
    echo "有批量/对局进程在跑（pgrep 匹配 mj.bot|arena_suite|round_batch|arena2.py|s3_batch|s2_batch），先等它结束再装。"
    exit 1
fi
if grep -q "s1_wall_relax_enabled" mj/hu_strategy.py 2>/dev/null; then
    echo "已经装过（mj/hu_strategy.py 里有 s1_wall_relax_enabled），跳过打补丁。"
else
    BK="tools/.cache/s1_variants_backup"
    mkdir -p "$BK/mj" "$BK/tools"
    for f in mj/hu_strategy.py tools/arena2.py tools/arena_suite.sh tools/arena_suite_report.py; do cp "$f" "$BK/$f"; done
    if ! patch -p1 --dry-run < tools/patches/s1_variants.patch >/dev/null; then
        echo "补丁打不上（这些文件在补丁生成之后又被改过），没有改动任何文件。把这条消息发给我。"
        exit 1
    fi
    patch -p1 < tools/patches/s1_variants.patch
    (cd tools/patches/s1_variants_payload && find . -type f) | while read -r f; do
        mkdir -p "$(dirname "$f")"; cp "tools/patches/s1_variants_payload/$f" "$f"
    done
    echo "已装上；原文件备份在 $BK/"
fi
MJ_WEIGHTS_NO_FILE=1 python3 -m unittest tests.test_s1_variants tests.test_hu_strategy tests.test_rule_switches tests.test_ablation_switches tests.test_arena2_mc 2>&1 | tail -4
