#!/bin/bash
# S1（状态编码与数据集）批处理：一条命令按顺序跑；每步前用退出码卡住 bot 检查；汇总追加到 reports/s1_summary.txt
# （每步终端汇总 <=40 行，明细在文件里）；失败记录后继续；支持只跑指定步骤；--smoke 为极小规模。
#
#   bash tools/train/s1_batch.sh                  # 全量（预计 5-15 分钟）
#   bash tools/train/s1_batch.sh --smoke
#   bash tools/train/s1_batch.sh check dataset    # 只跑指定步骤（--force 重跑已成功的）
#   MJ_JOBS=8 bash tools/train/s1_batch.sh
#
# 步骤：
#   tests    tests/test_nn_encode.py
#   check    check_encoder.py：1 万个真实快照 vs 独立参考实现 + 可逆字段还原 + 动作往返（S1 验收）
#   dataset  build_dataset.py：全部玩家全部决策 -> tools/.cache/train/parts/{train,val}/（按房间切分，可续跑）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
SUMMARY="reports/s1_summary.txt"
STATUS_FILE="tools/.cache/s1_batch_status.txt"
mkdir -p reports tools/.cache
. "${ROOT}/tools/cloud/env.sh"   # 可移植：IS_MAC / ncpu / RUN / EFF
SMOKE=0; FORCE=0; STEPS=""
for arg in "$@"; do
    case "$arg" in
        --smoke) SMOKE=1 ;;
        --force) FORCE=1 ;;
        *) STEPS="${STEPS} ${arg}" ;;
    esac
done
MODE="full"; [ "${SMOKE}" = "1" ] && MODE="smoke"
if [ -z "${STEPS}" ]; then STEPS="tests check dataset"; : > "${SUMMARY}"; else touch "${SUMMARY}"; fi
JOBS="${MJ_JOBS:-6}"
log() { echo "$*" | tee -a "${SUMMARY}"; }
check_bot() {
    if pgrep -f "mj.bot" >/dev/null; then
        echo "bot 在跑（pgrep -f mj.bot 有匹配），停止，不执行任何步骤。" | tee -a "${SUMMARY}"
        exit 1
    fi
}
step_done() {
    [ "${FORCE}" = "1" ] && return 1
    [ -f "${STATUS_FILE}" ] || return 1
    grep -q "^${1}	${MODE}	success	" "${STATUS_FILE}"
}
has_step() { case " ${STEPS} " in *" ${1} "*) return 0 ;; *) return 1 ;; esac; }
run_step() {
    step_id="$1"; name="$2"; shift 2
    if step_done "${step_id}"; then
        log "=== [${step_id}] 跳过（${MODE} 模式下已成功，--force 可重跑） ==="
        return
    fi
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
if [ "${SMOKE}" = "1" ]; then
    log "S1 批处理（smoke）开始：$(date)"
else
    log "S1 批处理（full）开始：$(date)，步骤：${STEPS}，MJ_JOBS=${JOBS}"
    log "预计耗时：tests <1 分钟；check 1-3 分钟；dataset 5-15 分钟（--keep 0.25，可 --max-minutes 续跑）；合计约 10-20 分钟"
fi
has_step tests && run_step tests "test_nn_encode" python3 -m unittest tests.test_nn_encode
has_step check && {
    if [ "${SMOKE}" = "1" ]; then run_step check "check_encoder 冒烟" python3 tools/train/check_encoder.py --smoke
    else run_step check "check_encoder（1 万个真实快照）" ${RUN} python3 tools/train/check_encoder.py --n 10000 --max-minutes 20; fi
}
has_step dataset && {
    if [ "${SMOKE}" = "1" ]; then run_step dataset "build_dataset 冒烟" python3 tools/train/build_dataset.py --smoke
    else run_step dataset "build_dataset 全量" ${RUN} python3 tools/train/build_dataset.py --jobs "${JOBS}" --max-minutes 60; fi
}
log ""
log "跑完：$(date)；汇总 ${SUMMARY}；数据集分片 tools/.cache/train/parts*/"
