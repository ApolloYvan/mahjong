#!/bin/sh
# 阶段一收尾的唯一交付命令：按顺序跑完 docs/IMPL_PHASE1_WRAP.md 里剩下的
# 全部验收项。每一步跑之前都用退出码卡住 bot 检查（不是打印一下看看），
# bot 在跑就整体退出，不继续。每一步的完整明细写到各自的文件里，终端只
# 累加一个 ≤40 行的汇总块到 reports/phase1_summary.txt。某一步失败记录下来
# 继续跑下一步，不中断整条流水线。
#
#   bash tools/phase1_batch.sh                    # 全量，预计耗时见下
#   bash tools/phase1_batch.sh --smoke             # 冒烟：每步都用极小规模，总共 <2 分钟
#   bash tools/phase1_batch.sh arena calibrate gap # 只跑指定步骤（名字见 STEP 行）
#   bash tools/phase1_batch.sh --smoke arena       # 两者可以组合
#
# 步骤名：replay / snapshot / arena / calibrate / gap / timing / tests
#
# 续跑：已经成功跑过的步骤（同一个 --smoke/全量模式下）默认跳过，状态记在
# tools/.cache/phase1_batch_status.txt；加 --force 忽略这个记录强制重跑。
#
# 预计总耗时（全量）：主要花在 arena2 A/A（按下面探测到的吞吐量自动换算
# 局数，目标控制在 2 小时以内）+ sim_snapshot_check 全量（历史上几分钟
# 量级，取决于日志总量）+ sim_calibrate/gap_breakdown（几分钟到十几分钟，
# 取决于语料规模）。跑完把 reports/phase1_summary.txt 贴回来。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SUMMARY="reports/phase1_summary.txt"
STATUS_FILE="tools/.cache/phase1_batch_status.txt"
mkdir -p reports tools/.cache
. "${ROOT}/tools/cloud/env.sh"   # 可移植：IS_MAC / ncpu / RUN / EFF（Linux 上没有 caffeinate/taskpolicy）

SMOKE=0
FORCE=0
STEPS=""
for arg in "$@"; do
    case "$arg" in
        --smoke) SMOKE=1 ;;
        --force) FORCE=1 ;;
        *) STEPS="$STEPS $arg" ;;
    esac
done
MODE="full"
if [ "$SMOKE" = "1" ]; then MODE="smoke"; fi

if [ -z "$STEPS" ]; then
    STEPS="replay snapshot arena calibrate gap timing tests"
    : > "$SUMMARY"   # 跑全套（没有指定步骤）才清空汇总文件；只跑指定步骤时续写，不冲掉之前的记录
else
    touch "$SUMMARY"
fi

log() { echo "$*" | tee -a "$SUMMARY"; }

check_bot() {
    if pgrep -f "mj.bot" >/dev/null; then
        echo "bot 在跑（pgrep -f mj.bot 有匹配），停止整条流水线，不执行任何步骤。" | tee -a "$SUMMARY"
        exit 1
    fi
}

step_done() {
    # 之前用同一个 MODE 成功跑过这个步骤，且没有 --force，就跳过。
    [ "$FORCE" = "1" ] && return 1
    [ -f "$STATUS_FILE" ] || return 1
    grep -q "^$1	$MODE	success	" "$STATUS_FILE"
}

mark_status() {
    echo "$1	$MODE	$2	$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATUS_FILE"
}

run_step() {
    step_id="$1"; name="$2"; shift 2
    if step_done "$step_id"; then
        log "=== [$step_id] 跳过（$MODE 模式下之前已经成功过，--force 可强制重跑） ==="
        return
    fi
    check_bot
    echo "=== [$(date '+%H:%M:%S')] 开始：$name（$MODE） ===" | tee -a "$SUMMARY"
    start=$(date +%s)
    if "$@" >>"$SUMMARY" 2>&1; then
        status="成功"
        mark_status "$step_id" "success"
    else
        code=$?
        status="失败（退出码 ${code}，看上面这一段输出定位原因，不影响后续步骤）"
        mark_status "$step_id" "fail"
    fi
    elapsed=$(( $(date +%s) - start ))
    echo "=== [$name] ${status}，耗时 ${elapsed}s ===" | tee -a "$SUMMARY"
}

has_step() {
    case " $STEPS " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

log "阶段一收尾批处理开始：$(date)（模式=$MODE，步骤=$STEPS）"
log "每一步跑之前都会重新检查 bot 是否在跑；某一步失败会记录原因并继续下一步。"

# ---------------------------------------------------------------- replay
if has_step replay; then
    if [ "$SMOKE" = "1" ]; then
        run_step replay "sim_replay_check 冒烟" python3 tools/sim_replay_check.py --limit 20
    else
        run_step replay "sim_replay_check 全量（带缓存）" python3 tools/sim_replay_check.py
    fi
fi

# ---------------------------------------------------------------- snapshot
if has_step snapshot; then
    if [ "$SMOKE" = "1" ]; then
        run_step snapshot "sim_snapshot_check 冒烟" python3 tools/sim_snapshot_check.py --limit 20
    else
        run_step snapshot "sim_snapshot_check 全量（3c 精确 seq 对齐，带缓存）" python3 tools/sim_snapshot_check.py
    fi
fi

# ---------------------------------------------------------------- arena
if has_step arena; then
    if [ "$SMOKE" = "1" ]; then
        run_step arena "arena2 冒烟（smoke 子命令）" python3 tools/arena2.py smoke
    else
        check_bot
        log "=== [$(date '+%H:%M:%S')] 探测 arena2 单种子耗时，用来把 A/A 局数换算到 2 小时预算内 ==="
        BENCH_OUT=$(python3 tools/arena2.py bench --seconds 20 2>&1)
        echo "$BENCH_OUT" >> "$SUMMARY"
        PER_HOUR=$(echo "$BENCH_OUT" | grep -o '每核每小时局数 [0-9]*' | grep -o '[0-9]*' | head -1)
        PER_HOUR=${PER_HOUR:-2000}   # 探测失败时退化到一个保守估计，不让整条流水线卡死
        JOBS=$(ncpu)
        # 每个种子=8局；目标：2v2(6种排法)+1v3(4种排法)共10种排法/种子，2小时预算，
        # --jobs 用探测到的核数。局数换算：matches = 目标秒数 * per_hour_rounds / 3600 / (10*8/JOBS 的等效串行局数)
        # 简化：直接按 per_hour（单核）估算单种子墙钟时间，再按 JOBS 并行换算种子数。
        SEC_PER_SEED=$(python3 -c "print(max(1.0, 8*10*3600.0/${PER_HOUR}))" 2>/dev/null || echo 30)
        MATCHES_2V2=$(python3 -c "print(max(5, int(7200*${JOBS}/(2*${SEC_PER_SEED}))))" 2>/dev/null || echo 50)
        MATCHES_1V3=$(( MATCHES_2V2 / 2 ))
        log "探测结果：单核 ${PER_HOUR} 局/小时，--jobs ${JOBS}；2v2 用 ${MATCHES_2V2} 个种子，1v3 用 ${MATCHES_1V3} 个种子（各自目标 <=1 小时）"

        run_step arena_seatbias_2v2 "arena2 seatbias A/A 2v2（对战平台自检）" \
            python3 tools/arena2.py seatbias --matches "$MATCHES_2V2" --jobs "$JOBS" --layout 2v2
        run_step arena_seatbias_1v3 "arena2 seatbias A/A 1v3（对战平台自检，比赛真实局面）" \
            python3 tools/arena2.py seatbias --matches "$MATCHES_1V3" --jobs "$JOBS" --layout 1v3
        run_step arena_bench_perf "arena2 bench 性能核（默认调度）" python3 tools/arena2.py bench --seconds 60
        if [ -n "${EFF}" ]; then
            run_step arena_bench_eff "arena2 bench 能效核（taskpolicy -c background）" \
                ${EFF} python3 tools/arena2.py bench --seconds 60
        else
            log "=== [arena_bench_eff] 跳过：不是 macOS（实战测速只在 macOS 跑） ==="
        fi
    fi
fi

# ---------------------------------------------------------------- calibrate
if has_step calibrate; then
    if [ "$SMOKE" = "1" ]; then
        run_step calibrate "sim_calibrate 冒烟" python3 tools/sim_calibrate.py --arena-seeds 2 --jobs 2
    else
        run_step calibrate "sim_calibrate（G3a 三组校准）" python3 tools/sim_calibrate.py --arena-seeds 400
    fi
fi

# ---------------------------------------------------------------- gap
if has_step gap; then
    if [ "$SMOKE" = "1" ]; then
        run_step gap "gap_breakdown 冒烟" python3 tools/gap_breakdown.py --bootstrap 5 --jobs 2
    else
        run_step gap "gap_breakdown（防选择偏差新版）" python3 tools/gap_breakdown.py
    fi
fi

# ---------------------------------------------------------------- timing
if has_step timing; then
    if [ "$SMOKE" = "1" ]; then
        run_step timing "timing_chain 冒烟" python3 tools/timing_chain.py --limit-files 2
    else
        run_step timing "timing_chain（时间链实测）" python3 tools/timing_chain.py
    fi
fi

# ---------------------------------------------------------------- tests
if has_step tests; then
    if [ "$SMOKE" = "1" ]; then
        # tests/test_live_preflight.py 单独排除：它的 7 个用例各自 fork 一次
        # 子进程跑另外 7 个测试模块（见 tools/live_preflight.py::
        # check_targeted_production_tests），单独跑一次就要 1-2 分钟，跟这次
        # 改动无关（已经单独验证过，见交付说明），不能让它把 smoke 预算
        # 吃光。全量模式（不加 --smoke）仍然跑完整 discover，包含它。
        SMOKE_MODULES=$(python3 -c "
import glob, os
for p in sorted(glob.glob('tests/test_*.py')):
    name = os.path.basename(p)[:-3]
    if name != 'test_live_preflight':
        print('tests.' + name)
")
        run_step tests "单元测试（smoke，排除 test_live_preflight，见脚本注释）" \
            env MJ_WEIGHTS_NO_FILE=1 python3 -m unittest $SMOKE_MODULES
    else
        run_step tests "全部单元测试" env MJ_WEIGHTS_NO_FILE=1 python3 -m unittest discover tests
    fi
fi

log ""
log "跑完：$(date)"
log "完整汇总见 ${SUMMARY}；各步骤自己的明细文件路径已经打印在上面对应的段落里（都在 reports/ 或 tools/.cache/ 下）。"
