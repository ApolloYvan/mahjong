# 可移植环境探测（macOS / Linux 云主机），被各 batch 脚本 source。POSIX sh 兼容。
#   IS_MAC=1/0    ncpu()    RUN（caffeinate/nice，有才加）    EFF（taskpolicy 能效核前缀，只有 macOS 有）
IS_MAC=0
[ "$(uname -s)" = "Darwin" ] && IS_MAC=1
ncpu() { nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4; }
RUN=""
command -v caffeinate >/dev/null 2>&1 && RUN="caffeinate -i"
command -v nice >/dev/null 2>&1 && RUN="${RUN} nice -n 15"
EFF=""
if [ "${IS_MAC}" = "1" ] && command -v taskpolicy >/dev/null 2>&1; then EFF="taskpolicy -c background"; fi
