#!/bin/sh
# 阶段二（离线版）批处理：一条命令按顺序跑完验收项。每一步之前都用退出码卡住 bot 检查；
# 每步输出（各工具自己保证 <=40 行汇总、明细写文件）追加到 reports/phase2_summary.txt；
# 某一步失败记录后继续下一步（依赖失败的步骤会被跳过并写明原因）。
#
#   bash tools/phase2_batch.sh                        # 全量
#   bash tools/phase2_batch.sh --smoke                # 冒烟：每步极小规模，总共 <2 分钟
#   bash tools/phase2_batch.sh seatbias simcal        # 只跑指定步骤；--smoke/--force 可组合
#
# 步骤（括号内是依赖）：
#   rules     rules_fix_impact（跨花色顺子修复影响面）
#   equiv     mc_check slim-equiv（1 万局对照 RoundEngine）
#   speed     mc_check slim-speed（性能核 + taskpolicy 能效核）
#   seatbias  arena2 seatbias 2v2 + 1v3（碰/吃后多摸牌的驱动 bug 修复后重跑，局数按 1 小时控制）
#   simcal    sim_calibrate（校准标靶 reports/sim_calibrate.json）
#   gap       gap_breakdown
#   mccal     mc_check calibrate（依赖 simcal）-> models/mc_opp_params.json
#   offline   mc_offline 全量（依赖 mccal；没有校准参数时 mc_offline 自己会拒绝运行）
#
# 已成功的步骤（同一模式下）默认跳过，状态在 tools/.cache/phase2_batch_status.txt；--force 强制重跑。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SUMMARY="reports/phase2_summary.txt"
STATUS_FILE="tools/.cache/phase2_batch_status.txt"
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
    STEPS="rules equiv speed seatbias simcal gap mccal offline"
    : > "$SUMMARY"
else
    touch "$SUMMARY"
fi

JOBS=${MJ_JOBS:-$(ncpu)}
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
dep_ok() { [ -f "$STATUS_FILE" ] && grep -q "^$1	$MODE	success	" "$STATUS_FILE"; }
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

# 1 小时预算下 arena2 seatbias 的种子数（按单核每种子约 8s(2v2)/5s(1v3) 估，--jobs=核数）
SB_2V2=$(( 1800 * JOBS / 8 )); SB_1V3=$(( 1800 * JOBS / 5 ))

if [ "$SMOKE" = "1" ]; then
    log "阶段二批处理（smoke）开始：$(date)；预计总耗时 <2 分钟（每步极小规模）"
else
    log "阶段二批处理（full）开始：$(date)，步骤：$STEPS"
    log "预计耗时（${JOBS} 核估计，量级而非保证）："
    log "  rules    ~15-25 分钟（全语料回放新旧规则 + 扫日志）"
    log "  equiv    ~1 分钟（1 万局 slim 对照 RoundEngine）"
    log "  speed    ~2.5 分钟（性能核 60s + 能效核 60s + 启动）"
    log "  seatbias ~60 分钟（2v2 ${SB_2V2} 种子 + 1v3 ${SB_1V3} 种子，各约 30 分钟）"
    log "  simcal   ~10-15 分钟（真实语料扫描 + arena2 自对局 400 种子）"
    log "  gap      ~5-10 分钟"
    log "  mccal    ~5-8 分钟（72 组参数 x 1500 局 + 补足测量 11 轮 x 3 万局 slim 自对弈）"
    log "  offline  ~30-60 分钟（<=1800 个决策点 x 512 局 x 多候选）"
    log "  合计     约 2.5-3.5 小时"
fi

has_step rules && {
    if [ "$SMOKE" = "1" ]; then run_step rules "rules_fix_impact 冒烟" python3 tools/rules_fix_impact.py --limit 4 --jobs 2
    else run_step rules "rules_fix_impact 全量" python3 tools/rules_fix_impact.py --jobs "$JOBS"; fi
}
has_step equiv && {
    if [ "$SMOKE" = "1" ]; then run_step equiv "slim-equiv 冒烟(300 局)" python3 tools/mc_check.py slim-equiv --limit 300
    else run_step equiv "slim-equiv(10000 局)" python3 tools/mc_check.py slim-equiv --limit 10000; fi
}
has_step speed && {
    SECS=60; [ "$SMOKE" = "1" ] && SECS=4
    if [ "$IS_MAC" = "1" ]; then
        run_step speed_perf "slim-speed 性能核" python3 tools/mc_check.py slim-speed --seconds "$SECS"
        [ -n "$EFF" ] && run_step speed_eff "slim-speed 能效核(taskpolicy -c background)" \
            $EFF python3 tools/mc_check.py slim-speed --seconds "$SECS"
    else
        log "=== [speed] 跳过：不是 macOS（实战测速只在 macOS 跑） ==="
    fi
}
has_step seatbias && {
    if [ "$SMOKE" = "1" ]; then
        run_step seatbias_2v2 "arena2 seatbias 2v2 冒烟" python3 tools/arena2.py seatbias --matches 2 --jobs 2 --layout 2v2 --no-cache
        run_step seatbias_1v3 "arena2 seatbias 1v3 冒烟" python3 tools/arena2.py seatbias --matches 2 --jobs 2 --layout 1v3 --no-cache
    else
        run_step seatbias_2v2 "arena2 seatbias 2v2（${SB_2V2} 种子）" python3 tools/arena2.py seatbias --matches "$SB_2V2" --jobs "$JOBS" --layout 2v2
        run_step seatbias_1v3 "arena2 seatbias 1v3（${SB_1V3} 种子）" python3 tools/arena2.py seatbias --matches "$SB_1V3" --jobs "$JOBS" --layout 1v3
    fi
}
has_step simcal && {
    if [ "$SMOKE" = "1" ]; then run_step simcal "sim_calibrate 冒烟" python3 tools/sim_calibrate.py --arena-seeds 2 --jobs 2
    else run_step simcal "sim_calibrate" python3 tools/sim_calibrate.py --arena-seeds 400 --jobs "$JOBS"; fi
}
has_step gap && {
    if [ "$SMOKE" = "1" ]; then run_step gap "gap_breakdown 冒烟" python3 tools/gap_breakdown.py --bootstrap 5 --jobs 2
    else run_step gap "gap_breakdown" python3 tools/gap_breakdown.py --jobs "$JOBS"; fi
}
has_step mccal && {
    if ! dep_ok simcal; then
        log "=== [mccal] 跳过：依赖的 simcal 在 $MODE 模式下没有成功（先跑 simcal） ==="
    elif [ "$SMOKE" = "1" ]; then
        run_step mccal "mc_check calibrate 冒烟(--quick，产物写 tools/.cache，不碰 models/mc_opp_params.json)" \
            python3 tools/mc_check.py calibrate --quick --rounds 100 --hazard-rounds 300 --refine 1 --jobs 2
    else
        run_step mccal "mc_check calibrate(slim 策略拟合 + 胡牌率补足)" python3 tools/mc_check.py calibrate --jobs "$JOBS"
    fi
}
has_step offline && {
    if ! dep_ok mccal; then
        log "=== [offline] 跳过：依赖的 mccal 在 $MODE 模式下没有成功（先跑 mccal） ==="
    elif [ "$SMOKE" = "1" ]; then
        run_step offline "mc_offline 冒烟（mccal 冒烟产物 + 补足表，仅验证链路）" python3 tools/mc_offline.py --params tools/.cache/mc_opp_params_smoke.json \
            --max-points 6 --per-file 2 --n 64 --pilot 16 --topk 3 --jobs 2 --limit-files 150 --no-cache
    else
        run_step offline "mc_offline 全量" python3 tools/mc_offline.py --jobs "$JOBS"
    fi
}

log ""
log "跑完：$(date)；汇总 ${SUMMARY}，各步明细文件路径见上面对应段落（reports/ 或 tools/.cache/）。"
