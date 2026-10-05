#!/bin/sh
# 从云主机拉回结果。默认只拉"汇总"（几十 KB）：reports/*.txt、reports/*summary*、校准/模型小文件。
# 明细（reports/arena2_ab_*.json 等，可能几百 MB，公网下行按流量计费）只在 FULL=1 时拉。
# 用法：CLOUD=user@host bash tools/cloud/sync_down.sh            # 汇总
#       CLOUD=user@host FULL=1 bash tools/cloud/sync_down.sh     # 汇总 + 明细 + 搜索结果
set -eu
: "${CLOUD:?先设置 CLOUD=user@host}"
DEST="${DEST:-mahjong}"
cd "$(dirname "$0")/../.."
mkdir -p reports/cloud
rsync -az --include='*.txt' --include='*summary*' --include='*.tsv' --exclude='*' \
  "$CLOUD:$DEST/reports/" ./reports/cloud/
rsync -az --include='nn_*.json' --include='mc_opp_params.json' --exclude='*' "$CLOUD:$DEST/models/" ./models/ 2>/dev/null || true
echo "汇总已拉到 reports/cloud/："; ls -la reports/cloud | tail -n +2
if [ "${FULL:-0}" = "1" ]; then
    rsync -az --progress "$CLOUD:$DEST/reports/" ./reports/cloud/
    mkdir -p tools/.cache/search
    rsync -az --progress "$CLOUD:$DEST/tools/.cache/search/results.jsonl" ./tools/.cache/search/ 2>/dev/null || echo "云端没有搜索结果"
    rsync -az --progress "$CLOUD:$DEST/tools/overlays/" ./tools/overlays/
fi
