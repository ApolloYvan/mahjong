#!/bin/bash
# 一次跑完所有 A/B（评估器验收 V1/V2 + 庄家 D1-D3 + 吃碰杠 C1/C2），1v3 + 2v2，同牌轮座、8 局整场、按种子块聚类。
# 每组默认 --max-minutes 40（在 1v3 与 2v2 之间平分；SUITE_MIN 可改），到点用已完成的种子出结果。
# macOS / Linux 通用；可断线续跑（arena2 按种子缓存，状态文件记已完成的组）。每步前用退出码卡住 bot 检查。
#
#   MJ_JOBS=6 bash tools/arena_suite.sh                         # 本机：7 组 x 40 分钟 ≈ 4.7 小时（时间封顶，种子数少，CI 会宽）
#   MJ_JOBS=$(nproc) bash tools/cloud/run_detached.sh suite bash tools/arena_suite.sh   # 云上：同样 4.7 小时墙钟，种子数多约 20 倍
#   bash tools/arena_suite.sh --smoke                          # 冒烟
#   bash tools/arena_suite.sh V1 V2                            # 只跑指定组（--force 重跑已成功的）
#   SUITE_MIN=20 SUITE_LAYOUTS="1v3" bash tools/arena_suite.sh D1   # 改每组分钟数/只跑某种排法
# 组（A = 改动的 overlay，B = 现行配置，overlay 文件在 tools/overlays/suite/）：
#   V1 rule_decline_joker_hold_enabled=0     平台应判现行更好（实战已证 S1 正收益）
#   V2 joker_nonbaotou_hu_disabled=1         平台应判现行显著更好（复现 243:0 旧 bug）
#   D1 dealer_route_enabled=0   D2 dealer_slow1_enabled=0   D3 s1_dealer_enabled=0
#   C1 rule_tenpai_peng_gang_relax_enabled=0   C2 gang_strict_enabled=1
#   E1 s1_wall_relax_enabled=1（墙尾门槛 8->4）  E2 s1_one_step_enabled=1（差一步转爆头）  E3 s1_ev_enabled=1（p̂>p*+δ）——S1 放宽变体，
#      要显式点名：MJ_JOBS=6 bash tools/arena_suite.sh E1 E2 E3（只跑 1v3，每组 20 分钟，可续跑；输出里带 S1 触发次数/胡率/净得分）
# 汇总 reports/arena_suite.txt（tools/arena_suite_report.py 生成，终端 <=40 行）；各组明细 reports/suite/*.json、reports/arena2_ab_*.json。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SUMMARY="reports/arena_suite_log.txt"
STATUS_FILE="tools/.cache/arena_suite_status.txt"
mkdir -p reports/suite tools/.cache
. "${ROOT}/tools/cloud/env.sh"   # IS_MAC / ncpu / RUN / EFF
SMOKE=0; FORCE=0; GROUPS_SEL=""
for arg in "$@"; do
    case "$arg" in
        --smoke) SMOKE=1 ;;
        --force) FORCE=1 ;;
        *) GROUPS_SEL="${GROUPS_SEL} ${arg}" ;;
    esac
done
MODE="full"; [ "${SMOKE}" = "1" ] && MODE="smoke"
[ -z "${GROUPS_SEL}" ] && GROUPS_SEL="V1 V2 D1 D2 D3 C1 C2"   # E1-E3（S1 放宽）要显式点名：tools/arena_suite.sh E1 E2 E3
JOBS="${MJ_JOBS:-6}"
SUITE_MIN="${SUITE_MIN:-40}"
SUITE_LAYOUTS="${SUITE_LAYOUTS:-1v3 2v2}"
NL=0; for l in ${SUITE_LAYOUTS}; do NL=$((NL+1)); done
PER_LAYOUT=$(python3 -c "print(${SUITE_MIN}/${NL})")
log() { echo "$*" | tee -a "${SUMMARY}"; }
check_bot() {
    if pgrep -f "mj.bot" >/dev/null; then
        echo "bot 在跑（pgrep -f mj.bot 有匹配），停止，不执行任何步骤。" | tee -a "${SUMMARY}"
        exit 1
    fi
}
done_ok() {
    [ "${FORCE}" = "1" ] && return 1
    [ -f "${STATUS_FILE}" ] && grep -q "^${1}	${MODE}	success	" "${STATUS_FILE}"
}
log "arena_suite（${MODE}）开始：$(date)；组：${GROUPS_SEL}；每组 ${SUITE_MIN} 分钟（排法 ${SUITE_LAYOUTS}，每个 ${PER_LAYOUT} 分钟）；MJ_JOBS=${JOBS}；系统 $(uname -s)"
for g in ${GROUPS_SEL}; do
    A="tools/overlays/suite/${g}_a.json"
    [ -f "${A}" ] || { log "=== [${g}] 没有 overlay ${A}，跳过 ==="; continue; }
    G_LAYOUTS="${SUITE_LAYOUTS}"; G_MIN="${PER_LAYOUT}"
    case "${g}" in E*) G_LAYOUTS="${E_LAYOUTS:-1v3}"; G_MIN="${E_MIN:-20}" ;; esac   # E 组：只跑 1v3，每组 20 分钟（E_LAYOUTS / E_MIN 可改）
    for layout in ${G_LAYOUTS}; do
        sid="${g}_${layout}"
        if done_ok "${sid}"; then log "=== [${sid}] 跳过（${MODE} 模式下已成功，--force 可重跑） ==="; continue; fi
        check_bot
        echo "=== [$(date '+%H:%M:%S')] 开始：${sid}（A=${A}，B=当前配置）===" | tee -a "${SUMMARY}"
        start=$(date +%s)
        if [ "${SMOKE}" = "1" ]; then
            ${RUN} python3 tools/arena2.py ab --a "${A}" --tag "${g}" --layout "${layout}" --matches 1 --rounds 1 --jobs 2 --no-cache --max-minutes 0.4 >>"${SUMMARY}" 2>&1
        else
            ${RUN} python3 tools/arena2.py ab --a "${A}" --tag "${g}" --layout "${layout}" --matches 100000 --jobs "${JOBS}" --max-minutes "${G_MIN}" >>"${SUMMARY}" 2>&1
        fi
        rc=$?
        [ "${rc}" = "0" ] && echo "${sid}	${MODE}	success	$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${STATUS_FILE}" || echo "${sid}	${MODE}	fail	$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${STATUS_FILE}"
        echo "=== [${sid}] 退出码 ${rc}，耗时 $(( $(date +%s) - start ))s ===" | tee -a "${SUMMARY}"
    done
done
python3 tools/arena_suite_report.py
