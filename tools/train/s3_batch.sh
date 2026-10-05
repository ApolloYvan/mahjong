#!/bin/bash
# S3（搜索改进器 + S2' "生产+修正"出牌模型）批处理。macOS / Linux 云主机通用；可在 tmux / nohup 下断线续跑
# （各步都有缓存/状态文件，重跑同一条命令只补没做完的；``tools/cloud/run_detached.sh`` 帮你 nohup 起）。
#
#   MJ_JOBS=6 bash tools/train/s3_batch.sh                 # 本机（macOS）
#   MJ_JOBS=$(nproc) S3_STATES=100000 S3_SEARCH_MIN=240 bash tools/train/s3_batch.sh   # 云主机（128 vCPU）
#   bash tools/train/s3_batch.sh --smoke                   # 冒烟（含多进程路径）
#   bash tools/train/s3_batch.sh search train              # 只跑指定步骤（--force 重跑已成功的）
#
# 步骤（括号内是依赖）：
#   bench    1 分钟实测吞吐：我方=生产的单次推演耗时 -> 每 1000 个状态在 64/128/256 vCPU 上要几小时（第一步）
#   tests    mc/nn/arena2/bot/hu_strategy/rule_switches 单元测试
#   diag     diag_shift.py：网络与生产的一致率，数据集快照 vs arena 快照（训练/实战分布偏差诊断；需要 models/nn_policy.json）
#   collect  抽 S3_STATES 个决策状态（有财神 50% / 门清 30% / 其它 20%）
#   search   搜索改进器（collect）：生产前 K 名候选，选择批/检验批独立，z>=2；--max-minutes S3_SEARCH_MIN，可续跑
#   masters  build_resid.py：高手标签数据（只作诊断：高手选择落在生产前 K 名里的比例；不再用于训练，训练目标是搜索给的"相对生产的优势"）
#   train    train_resid.py（search）：回归"相对生产的优势"（分/局），零初始化 -> models/nn_resid.json（新文件，不动 models/weights.json）
#   verify   verify_resid.py（train）：纯 Python vs torch 精度、耗时（性能核/能效核只在 macOS 测）
#   arena    arena2 ab 1v3：A = nn_resid_enabled，B = 当前配置，--max-minutes S3_ARENA_MIN（train）
# 环境变量：S3_STATES(4000) S3_SEARCH_MIN(90) S3_N_SEL(32) S3_N_TEST(128) S3_K(5) S3_ARENA_MIN(60) S3_CLOUD_SPEED(1.0)
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
SUMMARY="reports/s3_summary.txt"
STATUS_FILE="tools/.cache/s3_batch_status.txt"
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
if [ -z "${STEPS}" ]; then STEPS="bench tests diag collect search masters train verify arena"; : > "${SUMMARY}"; else touch "${SUMMARY}"; fi
export MJ_JOBS="${MJ_JOBS:-6}"
JOBS="${MJ_JOBS}"
S3_STATES="${S3_STATES:-4000}"; S3_SEARCH_MIN="${S3_SEARCH_MIN:-90}"; S3_N_SEL="${S3_N_SEL:-32}"; S3_N_TEST="${S3_N_TEST:-128}"
S3_K="${S3_K:-5}"; S3_ARENA_MIN="${S3_ARENA_MIN:-60}"; S3_CLOUD_SPEED="${S3_CLOUD_SPEED:-1.0}"
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
need() { if ! dep_ok "$2"; then log "=== [${1}] 跳过：依赖的 ${2} 在 ${MODE} 模式下没有成功 ==="; return 1; fi; return 0; }

if [ "${SMOKE}" = "1" ]; then
    export MJ_SEARCH_DIR="${ROOT}/tools/.cache/search_smoke"
    export MJ_NN_RESID_PATH="${ROOT}/tools/.cache/train/nn_resid_smoke.json"
    RESID_MODEL="tools/.cache/train/nn_resid_smoke.json"
    SM="--smoke"
    log "S3 批处理（smoke）开始：$(date)"
else
    RESID_MODEL="models/nn_resid.json"
    SM=""
    log "S3 批处理（full）开始：$(date)，步骤：${STEPS}，MJ_JOBS=${JOBS}，系统 $(uname -s)，状态数 ${S3_STATES}，搜索上限 ${S3_SEARCH_MIN} 分钟"
    log "预计耗时（本机 6 核量级）：bench 2 分钟；tests 1 分钟；diag ~20 分钟；collect 1-3 分钟；search ${S3_SEARCH_MIN} 分钟（上限，可续跑）；masters 30-60 分钟；train ~5 分钟；verify ~3 分钟；arena ${S3_ARENA_MIN} 分钟"
fi
SEARCH_DIR="${MJ_SEARCH_DIR:-tools/.cache/search}"
TEST_MODS="tests.test_nn_encode tests.test_nn_policy tests.test_nn_resid tests.test_search_s3 tests.test_mc_fast tests.test_mc_determinize tests.test_mc_rollout tests.test_mc_decide tests.test_mc_slim tests.test_mc_bot tests.test_arena2_mc tests.test_bot_state tests.test_hu_strategy tests.test_rule_switches"
run_tests() {
    MJ_WEIGHTS_NO_FILE=1 python3 -m unittest ${TEST_MODS} >tools/.cache/s3_tests.log 2>&1
    rc=$?
    tail -6 tools/.cache/s3_tests.log
    return $rc
}
run_bench() {
    python3 tools/train/search_s3.py collect --n 200 --max-minutes 2 || return 1
    if [ "${SMOKE}" = "1" ]; then
        python3 tools/train/search_s3.py bench --seconds 5 --jobs 2 --cloud-speed "${S3_CLOUD_SPEED}"
    else
        ${RUN} python3 tools/train/search_s3.py bench --seconds 60 --jobs "${JOBS}" --cloud-speed "${S3_CLOUD_SPEED}"
    fi
}
run_diag() {
    if [ ! -f models/nn_policy.json ]; then echo "没有 models/nn_policy.json（S2 网络），跳过诊断"; return 0; fi
    if [ "${SMOKE}" = "1" ]; then python3 tools/train/diag_shift.py --n 20 --max-minutes 0.4
    else ${RUN} python3 tools/train/diag_shift.py --n 1500 --max-minutes 20; fi
}

has_step bench && run_step bench "bench（1 分钟吞吐实测）" run_bench
has_step tests && run_step tests "单元测试" run_tests
has_step diag && run_step diag "diag_shift（训练/实战分布偏差）" run_diag
has_step collect && {
    if [ "${SMOKE}" = "1" ]; then run_step collect "collect 状态(冒烟 24 个)" python3 tools/train/search_s3.py collect --n 24 --max-minutes 1
    else run_step collect "collect 状态（${S3_STATES} 个）" python3 tools/train/search_s3.py collect --n "${S3_STATES}" --max-minutes 30; fi
}
has_step search && {
    if need search collect; then
        if [ "${SMOKE}" = "1" ]; then
            run_step search "search 冒烟（4 进程）" python3 tools/train/search_s3.py run --jobs 4 --limit 24 --k 4 --n-sel 4 --n-test 8 --max-minutes 0.7
        else
            run_step search "search（K=${S3_K} n_sel=${S3_N_SEL} n_test=${S3_N_TEST}，上限 ${S3_SEARCH_MIN} 分钟）" ${RUN} \
                python3 tools/train/search_s3.py run --jobs "${JOBS}" --k "${S3_K}" --n-sel "${S3_N_SEL}" --n-test "${S3_N_TEST}" --max-minutes "${S3_SEARCH_MIN}"
        fi
    fi
}
has_step masters && run_step masters "build_resid（高手标签）" ${RUN} python3 tools/train/build_resid.py ${SM} --jobs "${JOBS}" --k "${S3_K}" --max-minutes 60
has_step train && { need train search && {
    if [ "${SMOKE}" = "1" ]; then
        run_step train "train_resid 冒烟" python3 tools/train/train_resid.py --smoke --search "${SEARCH_DIR}/results.jsonl"
    else
        run_step train "train_resid（相对生产的优势回归）" ${RUN} python3 tools/train/train_resid.py --k "${S3_K}" --max-minutes 30
    fi
}; }
has_step verify && {
    if need verify train; then
        if [ "${IS_MAC}" = "1" ]; then
            run_step verify_perf "verify_resid 性能核" ${RUN} python3 tools/train/verify_resid.py ${SM} --label perf
            [ -n "${EFF}" ] && run_step verify_eff "verify_resid 能效核(taskpolicy -c background)" ${RUN} ${EFF} python3 tools/train/verify_resid.py ${SM} --label eff
        else
            run_step verify_perf "verify_resid 仅精度（非 macOS 不测耗时）" ${RUN} python3 tools/train/verify_resid.py ${SM} --label linux --skip-latency
        fi
    fi
}
has_step arena && {
    if need arena train; then
        if [ "${SMOKE}" = "1" ]; then
            run_step arena "arena2 ab 1v3 A=生产+修正(冒烟) B=当前" ${RUN} python3 tools/arena2.py ab --a tools/overlays/resid.json \
                --matches 1 --rounds 1 --jobs 2 --layout 1v3 --no-cache --max-minutes 0.8
        else
            run_step arena "arena2 ab 1v3 A=nn_resid_enabled B=当前配置（${S3_ARENA_MIN} 分钟）" ${RUN} python3 tools/arena2.py ab \
                --a tools/overlays/resid.json --matches 100000 --jobs "${JOBS}" --layout 1v3 --max-minutes "${S3_ARENA_MIN}"
        fi
    fi
}
log ""
log "跑完：$(date)；汇总 ${SUMMARY}；模型 ${RESID_MODEL}；搜索结果 ${SEARCH_DIR}/results.jsonl；arena 明细 reports/arena2_ab_*.json"
