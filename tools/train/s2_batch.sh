#!/bin/bash
# S2（模仿基线）批处理：一条命令按顺序跑；每步前用退出码卡住 bot 检查；汇总追加到 reports/s2_summary.txt
# （每步终端汇总 <=40 行，明细在文件里）；失败记录后继续（有依赖的步骤会跳过并写明原因）；--smoke 为极小规模。
#
#   MJ_JOBS=6 bash tools/train/s2_batch.sh                 # 全量（约 2-2.5 小时，大头是 arena 的 90 分钟）
#   bash tools/train/s2_batch.sh --smoke
#   bash tools/train/s2_batch.sh verify heads              # 只跑指定步骤（--force 重跑已成功的）
#
# 步骤（括号内是依赖）：
#   tests    tests/test_nn_encode / test_nn_policy / test_mc_* / test_arena2_mc / test_bot_state / test_hu_strategy / test_rule_switches
#   players  player_stats.py：按房间奇偶选高手（用另一半房间选人）
#   dataset  build_s2.py（players）：高手决策、出牌/胡飘全量、"过"降采样加权
#   train    train_s2.py（dataset）：torch 训练 -> models/nn_policy.json（新文件，不动 models/weights.json）
#   verify   verify_infer.py（train）：纯 Python vs torch 最大绝对差 <1e-5；性能核 + 能效核(taskpolicy -c background)耗时 p99<=50ms
#   heads    eval_heads.py（train）：三个头 top-1：网络 vs 生产 vs nn_discard，按有/无财神分层
#   arena    arena2 ab 1v3：A = nn_policy_enabled=1（只在 overlay 里），B = 当前配置，--max-minutes 90（train）
# 全量时 train/verify/heads/arena 需要 models/nn_policy.json；网络推理超时/异常/被生产校验否决一律回落生产。
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
SUMMARY="reports/s2_summary.txt"
STATUS_FILE="tools/.cache/s2_batch_status.txt"
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
if [ -z "${STEPS}" ]; then STEPS="tests players dataset train verify heads arena"; : > "${SUMMARY}"; else touch "${SUMMARY}"; fi
export MJ_JOBS="${MJ_JOBS:-6}"
JOBS="${MJ_JOBS}"
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
dep_ok() { [ -f "${STATUS_FILE}" ] && grep -q "^${1}	${MODE}	success	" "${STATUS_FILE}"; }
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
need() {   # need 步骤 依赖步骤
    if ! dep_ok "$2"; then log "=== [${1}] 跳过：依赖的 ${2} 在 ${MODE} 模式下没有成功 ==="; return 1; fi
    return 0
}

if [ "${SMOKE}" = "1" ]; then
    MODEL="tools/.cache/train/nn_policy_smoke.json"
    export MJ_NN_POLICY_PATH="${ROOT}/${MODEL}"
    SM="--smoke"
    log "S2 批处理（smoke）开始：$(date)"
else
    MODEL="models/nn_policy.json"
    SM=""
    log "S2 批处理（full）开始：$(date)，步骤：${STEPS}，MJ_JOBS=${JOBS}"
    log "预计耗时：tests ~1 分钟；players <1 分钟；dataset 3-10 分钟；train 5-20 分钟（可 --max-minutes 续跑）；verify ~4 分钟；heads 10-30 分钟；arena 90 分钟；合计约 2-2.5 小时"
fi
TEST_MODS="tests.test_nn_encode tests.test_nn_policy tests.test_mc_fast tests.test_mc_determinize tests.test_mc_rollout tests.test_mc_decide tests.test_mc_slim tests.test_mc_bot tests.test_arena2_mc tests.test_bot_state tests.test_hu_strategy tests.test_rule_switches"
run_tests() {
    MJ_WEIGHTS_NO_FILE=1 python3 -m unittest ${TEST_MODS} >tools/.cache/s2_tests.log 2>&1
    rc=$?
    tail -6 tools/.cache/s2_tests.log
    return $rc
}

has_step tests && run_step tests "单元测试" run_tests
has_step players && run_step players "player_stats（按房间奇偶选高手）" python3 tools/train/player_stats.py
has_step dataset && { need dataset players && run_step dataset "build_s2（高手决策数据集）" ${RUN} python3 tools/train/build_s2.py ${SM} --jobs "${JOBS}" --max-minutes 60; }
has_step train && { need train dataset && run_step train "train_s2（torch 模仿训练）" ${RUN} python3 tools/train/train_s2.py ${SM} --max-minutes 60; }
has_step verify && {
    if need verify train; then
        if [ "${IS_MAC}" = "1" ]; then
            run_step verify_perf "verify_infer 性能核" ${RUN} python3 tools/train/verify_infer.py ${SM} --label perf
            [ -n "${EFF}" ] && run_step verify_eff "verify_infer 能效核(taskpolicy -c background)" ${RUN} ${EFF} python3 tools/train/verify_infer.py ${SM} --label eff
        else
            run_step verify_perf "verify_infer 仅精度（非 macOS 不测耗时）" ${RUN} python3 tools/train/verify_infer.py ${SM} --label linux --skip-latency
        fi
    fi
}
has_step heads && { need heads train && run_step heads "eval_heads（三个头 top-1）" ${RUN} python3 tools/train/eval_heads.py ${SM} --max-minutes 30; }
has_step arena && {
    if need arena train; then
        if [ "${SMOKE}" = "1" ]; then
            run_step arena "arena2 ab 1v3 A=网络(冒烟) B=当前" ${RUN} python3 tools/arena2.py ab --a tools/overlays/nn.json \
                --matches 1 --rounds 1 --jobs 2 --layout 1v3 --no-cache --max-minutes 0.8
        else
            run_step arena "arena2 ab 1v3 A=nn_policy_enabled B=当前配置（90 分钟）" ${RUN} python3 tools/arena2.py ab \
                --a tools/overlays/nn.json --matches 100000 --jobs "${JOBS}" --layout 1v3 --max-minutes 90
        fi
    fi
}
log ""
log "跑完：$(date)；汇总 ${SUMMARY}；模型 ${MODEL}；arena 明细 reports/arena2_ab_*.json"
