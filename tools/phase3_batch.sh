#!/bin/sh
# 阶段三批处理：实时 MC（slim 推演）的验收。一条命令按顺序跑；每步之前用退出码卡住 bot 检查；
# 每步输出（各工具自己保证 <=40 行汇总、明细写文件）追加到 reports/phase3_summary.txt；
# 失败记录后继续下一步；支持只跑指定步骤。长任务一律前缀 caffeinate -i + nice -n 15。
#
#   bash tools/phase3_batch.sh                    # 全量（约 2.5 小时）
#   bash tools/phase3_batch.sh --smoke            # 冒烟，每步极小规模
#   bash tools/phase3_batch.sh abhu speedlive     # 只跑指定步骤；--force 重跑已成功的步骤
#   MJ_JOBS=8 bash tools/phase3_batch.sh          # 并行进程数，默认 6
#
# 步骤：
#   tests      全部 mc 相关单元测试 + test_bot_state/test_bot_cli_token/test_hu_strategy/test_rule_switches
#   aball      arena2 ab 1v3：A = 三个开关全开(tools/overlays/mc_all.json)，B = 当前配置，--max-minutes 100
#   abhu       arena2 ab 1v3：A = 只开 mc_hu_enabled(tools/overlays/mc_hu.json)，B = 当前配置，--max-minutes 40
#   speedlive  mc_check speed-live：真实快照上测 mc_override 耗时分布与回落率，性能核、能效核各一次
# aball/abhu/speedlive 需要 models/mc_opp_params.json（mc_check calibrate 的产物，mccal 步骤）；没有就跳过并写明。
# models/weights.json 不动。上线由用户看了 A/B 结果后决定。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SUMMARY="reports/phase3_summary.txt"
STATUS_FILE="tools/.cache/phase3_batch_status.txt"
mkdir -p reports tools/.cache
. "${ROOT}/tools/cloud/env.sh"   # 可移植：IS_MAC / ncpu / RUN / EFF

SMOKE=0; FORCE=0; STEPS=""
for arg in "$@"; do
    case "$arg" in
        --smoke) SMOKE=1 ;;
        --force) FORCE=1 ;;
        *) STEPS="$STEPS $arg" ;;
    esac
done
MODE="full"; [ "$SMOKE" = "1" ] && MODE="smoke"
if [ -z "$STEPS" ]; then
    STEPS="tests aball abhu speedlive"
    : > "$SUMMARY"
else
    touch "$SUMMARY"
fi

JOBS="${MJ_JOBS:-6}"
log() { echo "$*" | tee -a "$SUMMARY"; }
check_bot() {
    if pgrep -f "mj.bot" >/dev/null; then
        echo "bot 在跑（pgrep -f mj.bot 有匹配），停止，不执行任何步骤。" | tee -a "$SUMMARY"
        exit 1
    fi
}
step_done() {
    [ "$FORCE" = "1" ] && return 1
    [ -f "$STATUS_FILE" ] || return 1
    grep -q "^$1	$MODE	success	" "$STATUS_FILE"
}
mark_status() { echo "$1	$MODE	$2	$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS_FILE"; }
has_step() { case " $STEPS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

run_step() {
    step_id="$1"; name="$2"; shift 2
    if step_done "$step_id"; then
        log "=== [$step_id] 跳过（$MODE 模式下已成功，--force 可重跑） ==="
        return
    fi
    check_bot
    echo "=== [$(date '+%H:%M:%S')] 开始：${name}（${MODE}） ===" | tee -a "$SUMMARY"
    start=$(date +%s)
    if "$@" >>"$SUMMARY" 2>&1; then
        status="成功"; mark_status "$step_id" "success"
    else
        code=$?
        status="失败（退出码 ${code}，看上面输出；不影响后续无依赖的步骤）"; mark_status "$step_id" "fail"
    fi
    echo "=== [$name] ${status}，耗时 $(( $(date +%s) - start ))s ===" | tee -a "$SUMMARY"
}

if [ "$SMOKE" = "1" ]; then
    PARAMS_FILE="tools/.cache/mc_opp_params_smoke.json"
    export MJ_MC_OPP_PARAMS="$ROOT/$PARAMS_FILE"
else
    PARAMS_FILE="models/mc_opp_params.json"
fi
need_params() {
    if [ ! -f "$PARAMS_FILE" ]; then
        log "=== [${1}] 跳过：没有 ${PARAMS_FILE}（先跑 bash tools/phase2_batch.sh mccal$( [ "$SMOKE" = "1" ] && echo ' --smoke')；实时 MC 没有校准的对手参数会拒绝推演） ==="
        return 1
    fi
    return 0
}

TEST_MODS="tests.test_mc_fast tests.test_mc_determinize tests.test_mc_rollout tests.test_mc_decide tests.test_mc_slim tests.test_mc_bot tests.test_arena2_mc tests.test_bot_state tests.test_bot_cli_token tests.test_hu_strategy tests.test_rule_switches"
run_tests() {
    # 测试不读真实 models/weights.json（test_hu_strategy 没有自己设这个变量，不设会读到线上权重而误报）
    MJ_WEIGHTS_NO_FILE=1 python3 -m unittest $TEST_MODS >tools/.cache/phase3_tests.log 2>&1
    rc=$?
    tail -8 tools/.cache/phase3_tests.log
    [ "$rc" = "0" ] || echo "完整输出：tools/.cache/phase3_tests.log"
    return $rc
}

if [ "$SMOKE" = "1" ]; then
    log "阶段三批处理（smoke）开始：$(date)；每步极小规模，预计总耗时几分钟"
else
    log "阶段三批处理（full）开始：$(date)，步骤：${STEPS}，MJ_JOBS=${JOBS}"
    log "预计耗时（量级而非保证）："
    log "  tests     ~2-4 分钟"
    log "  aball     100 分钟（--max-minutes 100，到点用已完成的种子出结果）"
    log "  abhu      40 分钟（--max-minutes 40）"
    log "  speedlive ~3 分钟（性能核 60s + 能效核 60s + 启动）"
    log "  合计      约 2.5 小时"
fi

has_step tests && run_step tests "单元测试（mc 全部 + bot/hu_strategy/rule_switches）" run_tests

has_step aball && {
    if need_params aball; then
        if [ "$SMOKE" = "1" ]; then
            run_step aball "arena2 ab 1v3 A=全开(冒烟 n=96) B=当前" $RUN python3 tools/arena2.py ab \
                --a tools/overlays/mc_all_smoke.json --matches 1 --rounds 1 --jobs 2 --layout 1v3 --no-cache --max-minutes 0.8
        else
            run_step aball "arena2 ab 1v3 A=三个开关全开 B=当前配置（100 分钟）" $RUN python3 tools/arena2.py ab \
                --a tools/overlays/mc_all.json --matches 100000 --jobs "$JOBS" --layout 1v3 --max-minutes 100
        fi
    fi
}
has_step abhu && {
    if need_params abhu; then
        if [ "$SMOKE" = "1" ]; then
            run_step abhu "arena2 ab 1v3 A=只开 hu(冒烟 n=96) B=当前" $RUN python3 tools/arena2.py ab \
                --a tools/overlays/mc_all_smoke.json --matches 1 --rounds 1 --jobs 2 --layout 1v3 --no-cache --max-minutes 0.8
        else
            run_step abhu "arena2 ab 1v3 A=只开 mc_hu_enabled B=当前配置（40 分钟）" $RUN python3 tools/arena2.py ab \
                --a tools/overlays/mc_hu.json --matches 100000 --jobs "$JOBS" --layout 1v3 --max-minutes 40
        fi
    fi
}
has_step speedlive && {
    if need_params speedlive; then
        SECS=60; [ "$SMOKE" = "1" ] && SECS=6
        if [ "$IS_MAC" = "1" ]; then
            run_step speedlive_perf "speed-live 性能核" $RUN python3 tools/mc_check.py speed-live --seconds "$SECS" --label perf
            [ -n "$EFF" ] && run_step speedlive_eff "speed-live 能效核(taskpolicy -c background)" $RUN $EFF \
                python3 tools/mc_check.py speed-live --seconds "$SECS" --label eff
        else
            log "=== [speedlive] 跳过：不是 macOS（实战测速只在 macOS 跑） ==="
        fi
    fi
}

log ""
log "跑完：$(date)；汇总 ${SUMMARY}；arena2 明细 reports/arena2_ab_*.json，speed-live 明细 reports/mc_speed_live_*.json。"
