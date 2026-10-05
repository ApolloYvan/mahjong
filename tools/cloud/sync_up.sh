#!/bin/sh
# 把离线训练/评测需要的代码和数据推到云主机（只用于离线计算，云主机不连对局服务器）。
# 用法：CLOUD=user@host bash tools/cloud/sync_up.sh            （默认不传 logs/，5.8G 且多数步骤用不到）
#       CLOUD=user@host WITH_LOGS=1 bash tools/cloud/sync_up.sh  （需要 timing_chain / check_encoder 线上项时）
# 令牌与 cookie 一律排除，绝不离开本机。
set -eu
: "${CLOUD:?先设置 CLOUD=user@host}"
DEST="${DEST:-mahjong}"
cd "$(dirname "$0")/../.."
EXTRA=""
[ "${WITH_LOGS:-0}" = "1" ] || EXTRA="--exclude=/logs/"
rsync -az --progress \
  --exclude='.mj_token' --exclude='.mj_token_global' --exclude='portal_cookie.txt' \
  --exclude='.mj_stop' --exclude='__pycache__/' --exclude='.git/' \
  --include='/tools/.cache/' --include='/tools/.cache/train/' --include='/tools/.cache/train/masters.json' \
  --exclude='/tools/.cache/**' $EXTRA \
  ./ "$CLOUD:$DEST/"
ssh "$CLOUD" "cd $DEST && ls -a | grep -E 'mj_token|cookie' && echo '!!! 发现令牌文件，立即删除' || echo '令牌检查：云端无令牌文件'"
