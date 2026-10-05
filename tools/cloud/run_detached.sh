#!/bin/bash
# 在云主机上用 nohup 后台起一个批处理脚本，断线/关终端不影响；日志写 tools/.cache/detached/<名字>.log。
# 批处理脚本本身可断线续跑（状态文件 + 各步缓存），被杀/机器重启后重新执行同一条命令即可继续。
#   bash tools/cloud/run_detached.sh s3 bash tools/train/s3_batch.sh            # 名字 s3
#   MJ_JOBS=$(nproc) S3_STATES=100000 bash tools/cloud/run_detached.sh s3 bash tools/train/s3_batch.sh
#   bash tools/cloud/run_detached.sh status                                      # 看所有后台任务（pid / 是否还活着 / 日志末尾）
# 用 tmux 也行：tmux new -s s3 'MJ_JOBS=$(nproc) bash tools/train/s3_batch.sh'，断线后 tmux attach -t s3。
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
DIR="tools/.cache/detached"
mkdir -p "$DIR"
if [ "${1:-}" = "status" ]; then
    for pidf in "$DIR"/*.pid; do
        [ -f "$pidf" ] || continue
        name=$(basename "$pidf" .pid); pid=$(cat "$pidf")
        if kill -0 "$pid" 2>/dev/null; then st="运行中"; else st="已结束"; fi
        echo "[$name] pid=$pid $st；日志 $DIR/$name.log 末尾："
        tail -n 3 "$DIR/$name.log" 2>/dev/null | sed 's/^/    /'
    done
    exit 0
fi
name="$1"; shift
nohup setsid "$@" >"$DIR/$name.log" 2>&1 &
echo $! > "$DIR/$name.pid"
echo "已后台启动 [$name] pid=$(cat "$DIR/$name.pid")；日志 $DIR/$name.log；看进度：tail -f $DIR/$name.log 或 bash tools/cloud/run_detached.sh status"
