#!/bin/bash
# 本轮一条命令：单元测试 -> S1 庄闲拆分 -> A/B 套件（评估器验收 + 庄家 + 吃碰杠）-> 实战 A/B 报告自检。
# macOS / Linux 通用；每步前用退出码卡住 bot 检查；汇总追加 reports/round_summary.txt（每步 <=40 行）；失败继续；可断线续跑。
#
#   MJ_JOBS=6 bash tools/round_batch.sh                  # 本机：约 5 小时（套件 7 组 x 40 分钟是大头；时间封顶，种子少 CI 宽）
#   MJ_JOBS=$(nproc) bash tools/cloud/run_detached.sh round bash tools/round_batch.sh      # 云上：同样墙钟，种子数多约 20 倍
#   bash tools/round_batch.sh --smoke                    # 冒烟
#   bash tools/round_batch.sh s1split suite              # 只跑指定步骤（--force 重跑）；SUITE_MIN / SUITE_LAYOUTS 见 tools/arena_suite.sh
#
# 步骤：
#   tests    消融开关（默认值不变）/ 实战 A/B / 评估器相关单元测试 + hu_strategy / rule_switches / weights_overlay / mc / nn
#   s1split  tools/s1_split.py：S1 按庄/闲拆开的触发次数、最终胡率、净得分差（相对当场胡，按房聚类自助 CI）+ 庄家盈亏平衡成功率
#   suite    tools/arena_suite.sh：V1/V2（已知答案）、D1-D3（庄家）、C1/C2（吃碰杠）
#   live     tools/live_ab_report.py 自检（合成数据）。真实数据：python3 -m mj.bot --rooms N --ab A.json,B.json 打完、赛后拉事件，
#            再 python3 tools/live_ab_report.py
# S3 的"相对生产的优势"标签/训练在 tools/train/s3_batch.sh（重计算，建议云上跑）。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SUMMARY="reports/round_summary.txt"
STATUS_FILE="tools/.cache/round_batch_status.txt"
mkdir -p reports tools/.cache
. "${ROOT}/tools/cloud/env.sh"
SMOKE=0; FORCE=0; STEPS=""
for arg in "$@"; do
    case "$arg" in
        --smoke) SMOKE=1 ;;
        --force) FORCE=1 ;;
        *) STEPS="${STEPS} ${arg}" ;;
    esac
done
MODE="full"; [ "${SMOKE}" = "1" ] && MODE="smoke"
if [ -z "${STEPS}" ]; then STEPS="tests s1split suite live"; : > "${SUMMARY}"; else touch "${SUMMARY}"; fi
export MJ_JOBS="${MJ_JOBS:-6}"
FORCE_ARG=""; [ "${FORCE}" = "1" ] && FORCE_ARG="--force"
log() { echo "$*" | tee -a "${SUMMARY}"; }
check_bot() {
    if pgrep -f "mj.bot" >/dev/null; then
        echo "bot 在跑（pgrep -f mj.bot 有匹配），停止，不执行任何步骤。" | tee -a "${SUMMARY}"
        exit 1
    fi
}
step_done() {
    [ "${FORCE}" = "1" ] && return 1
    [ -f "${STATUS_FILE}" ] && grep -q "^${1}	${MODE}	success	" "${STATUS_FILE}"
}
has_step() { case " ${STEPS} " in *" ${1} "*) return 0 ;; *) return 1 ;; esac; }
run_step() {
    step_id="$1"; name="$2"; shift 2
    if step_done "${step_id}"; then log "=== [${step_id}] 跳过（${MODE} 模式下已成功，--force 可重跑） ==="; return; fi
    check_bot
    echo "=== [$(date '+%H:%M:%S')] 开始：${name}（${MODE}） ===" | tee -a "${SUMMARY}"
    start=$(date +%s)
    if "$@" >>"${SUMMARY}" 2>&1; then
        status="成功"; echo "${step_id}	${MODE}	success	$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${STATUS_FILE}"
    else
        code=$?
        status="失败（退出码 ${code}，看上面输出）"; echo "${step_id}	${MODE}	fail	$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${STATUS_FILE}"
    fi
    echo "=== [${name}] ${status}，耗时 $(( $(date +%s) - start ))s ===" | tee -a "${SUMMARY}"
}
if [ "${SMOKE}" = "1" ]; then log "round_batch（smoke）开始：$(date)"
else
    log "round_batch（full）开始：$(date)，步骤：${STEPS}，MJ_JOBS=${MJ_JOBS}，系统 $(uname -s)"
    log "预计耗时：tests ~2 分钟；s1split 3-10 分钟（扫 6GB 日志，结果缓存）；suite 7 组 x ${SUITE_MIN:-40} 分钟 ≈ $(python3 -c "print(round(7*${SUITE_MIN:-40}/60,1))") 小时；live <1 分钟；合计约 5 小时"
fi
TEST_MODS="tests.test_ablation_switches tests.test_live_ab tests.test_weights_overlay tests.test_arena2_mc tests.test_nn_encode tests.test_nn_policy tests.test_nn_resid tests.test_search_s3 tests.test_mc_decide tests.test_mc_slim tests.test_mc_bot tests.test_bot_state tests.test_hu_strategy tests.test_rule_switches"
run_tests() {
    MJ_WEIGHTS_NO_FILE=1 python3 -m unittest ${TEST_MODS} >tools/.cache/round_tests.log 2>&1
    rc=$?
    tail -6 tools/.cache/round_tests.log
    return $rc
}
has_step tests && run_step tests "单元测试" run_tests
has_step s1split && {
    if [ "${SMOKE}" = "1" ]; then run_step s1split "s1_split 冒烟" python3 tools/s1_split.py --smoke
    else run_step s1split "s1_split（全部日志）" ${RUN} python3 tools/s1_split.py --jobs "${MJ_JOBS}" --max-minutes 30; fi
}
has_step suite && {
    if [ "${SMOKE}" = "1" ]; then run_step suite "arena_suite 冒烟（V1 D1 C2）" bash tools/arena_suite.sh --smoke ${FORCE_ARG} V1 D1 C2
    else run_step suite "arena_suite" bash tools/arena_suite.sh ${FORCE_ARG}; fi
}
has_step live && run_step live "live_ab_report 自检（合成数据）" python3 tools/live_ab_report.py --smoke
log ""
log "跑完：$(date)；汇总 ${SUMMARY}；套件汇总 reports/arena_suite.txt；S1 明细缓存 tools/.cache/s1_split_rows.jsonl"
