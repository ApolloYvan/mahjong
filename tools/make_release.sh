#!/bin/sh
# 封版：生成提交用的源码包 release/qianyuan-<版本>/ 和 release/qianyuan-<版本>.zip（含 .git，<20MB）。
# 只放运行需要的东西：mj/ 源码 + models/ 顶层 json + 使用说明。绝不含令牌、cookie、日志、对局数据。
#   bash tools/make_release.sh v1.0
# 检查项（任一失败即中止）：bot 未在跑、编译、无第三方顶层 import、令牌扫描、实验开关全关、
# 命令行入口可用、用发布目录里的代码跑 arena2 自对弈冒烟、包体积 <20MB。
set -eu
cd "$(dirname "$0")/.."
VER=${1:-v1.0}
NAME=qianyuan-$VER
OUT=release/$NAME

if pgrep -f "mj.bot" >/dev/null 2>&1; then
    echo "!!! bot 正在运行，先停 bot 再封版"; exit 1
fi

echo "=== 1. 复制文件 -> $OUT ==="
rm -rf "$OUT" "release/$NAME.zip"
mkdir -p "$OUT/models"
rsync -a --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' mj "$OUT/"
cp models/*.json "$OUT/models/"
cp docs/SUBMISSION_README.md "$OUT/README.md"
cat > "$OUT/.gitignore" <<'EOF'
logs/
__pycache__/
*.pyc
.DS_Store
.mj_token
.mj_token_global
EOF
find "$OUT" -name '*.py' -exec perl -pi -e 's/\r$//' {} +     # CRLF -> LF
echo "  文件数 $(find "$OUT" -type f | wc -l | tr -d ' ')"

echo "=== 2. 编译 ==="
python3 -m compileall -q "$OUT/mj" >/dev/null
find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} +
echo "  通过"

echo "=== 3. 第三方依赖（顶层 import torch/numpy 一律不许）==="
if grep -rnE '^(import|from) +(torch|numpy|scipy)' "$OUT/mj"; then
    echo "!!! 发现顶层第三方 import"; exit 1
fi
echo "  通过"

echo "=== 4. 令牌/敏感文件 ==="
for t in .mj_token .mj_token_global portal_cookie.txt logs; do
    [ ! -e "$OUT/$t" ] || { echo "!!! 包里有 $t"; exit 1; }
done
HITS=$(cd "$OUT" && python3 -c "from mj.security import scan; print(scan('.'))")
[ "$HITS" = "[]" ] || { echo "!!! 令牌扫描命中：$HITS"; exit 1; }
for t in .mj_token .mj_token_global portal_cookie.txt; do
    if [ -s "$t" ] && grep -rqF "$(head -c 40 "$t")" "$OUT"; then
        echo "!!! 包里出现了 $t 的内容"; exit 1
    fi
done
echo "  通过"

echo "=== 5. 实验开关必须全关（MC/NN/S1 变体等）==="
(cd "$OUT" && python3 - <<'PY'
from mj.fit import load_weights
w = load_weights()
bad = {k: v for k, v in w.items() if (k.startswith(("nn_", "mc_")) and k.endswith("_enabled") and v)
       or (k in ("s1_wall_relax_enabled", "s1_one_step_enabled", "s1_ev_enabled", "gang_strict_enabled",
                 "dealer_slow1_enabled", "joker_nonbaotou_hu_disabled") and v)}
if bad:
    raise SystemExit("!!! 实验开关没关：%s" % bad)
print("  通过；生效的 rule_* 开关：%s" % sorted(k for k, v in w.items() if k.endswith("_enabled") and v))
PY
)

echo "=== 6. 命令行入口 ==="
(cd "$OUT" && python3 -m mj.bot --help >/dev/null)
echo "  通过"

echo "=== 7. 用发布目录里的代码跑自对弈冒烟（arena2 smoke，读发布包的 models/weights.json）==="
TMP=$(mktemp -d)
cp -R "$OUT/." "$TMP/"
mkdir -p "$TMP/tools"
cp tools/arena2.py "$TMP/tools/"
(cd "$TMP" && MJ_WEIGHTS_NO_FILE=0 python3 tools/arena2.py smoke) | tail -8
rm -rf "$TMP"

echo "=== 8. git 提交 + 打包 ==="
(cd "$OUT" && git init -q && git add -A \
    && git -c user.name="乾元用九" -c user.email="iwanclaude@outlook.com" commit -q \
       -m "乾元用九 $VER 提交版" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" \
    && echo "  commit $(git rev-parse --short HEAD)")
(cd release && zip -qr "$NAME.zip" "$NAME")
SIZE=$(wc -c < "release/$NAME.zip")
echo "  release/$NAME.zip：$((SIZE / 1024)) KB"
[ "$SIZE" -lt 20000000 ] || { echo "!!! 超过 20MB"; exit 1; }

echo
echo "封版完成：$OUT（含 .git）与 release/$NAME.zip。提交时把 README.md 内容贴到申报页面正文，附上 zip。"
