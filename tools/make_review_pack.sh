#!/bin/sh
# 生成给外部模型的评审资料包：review_pack/PACK.md（单文件，适合上传到聊天界面）。
# 内容：评审 prompt + 平台规则/交接文档 + 生产决策代码 + 实验与实战汇总 + 数据格式样例。
# 绝不包含令牌、cookie、原始线上日志。
#   bash tools/make_review_pack.sh
set -eu
cd "$(dirname "$0")/.."
OUT=review_pack/PACK.md
mkdir -p review_pack
: > "$OUT"

section() { printf '\n\n---\n\n# %s\n\n' "$1" >> "$OUT"; }
add_file() {   # add_file 路径 语言
    [ -f "$1" ] || { echo "（缺失：$1）" >> "$OUT"; return; }
    printf '\n## 文件：`%s`\n\n```%s\n' "$1" "$2" >> "$OUT"
    cat "$1" >> "$OUT"
    printf '\n```\n' >> "$OUT"
}

cat docs/EXTERNAL_REVIEW_PROMPT.md >> "$OUT"
cat >> "$OUT" <<'EOF'


---

# 资料包说明（请先读）

下面附上判断所需的全部背景，按重要性排序：
- A. 实战差距数据：最新 30 房，加上与高手的对比，是最重要的事实来源。
- B. 平台规则与工程交接文档。
- C. 生产决策代码：当前线上策略的完整实现。所有规则开关的证据和历史都写在 `mj/fit.py` 的 `DEFAULT_WEIGHTS` 注释里，线上生效值见 `models/weights.json`。
- D. 各次实验的汇总输出：V3 账本、离线 MC、实时 MC、模仿网络 S2、对手校准、差距分解。
- E. 早期离线分析报告：S1 弃胡转爆头、摸财神拒胡等发现的方法与数字。
- F. 数据格式样例：对局事件文件、线上决策日志，用来设计你提出的数据分析。

引用时请写明"文件:函数"。如果你认为某个结论需要额外的数据分析，请写出具体的分析方法（输入哪些数据、算什么、什么结果支持或否定假设），我们会执行并把结果回传给你。
EOF

section "A. 实战差距数据"
add_file reports/live30_dashboard.txt text

section "B. 平台规则与工程交接"
add_file docs/HANDOFF_CURRENT.md markdown
add_file docs/STRATEGY_MINING.md markdown

section "C. 生产决策代码（纯 Python 标准库）"
for f in models/weights.json mj/tiles.py mj/rules.py mj/shanten.py mj/melds.py mj/state.py \
         mj/strategy.py mj/ev.py mj/discard_features.py mj/joker_ev.py mj/route_ev.py \
         mj/responses.py mj/hu_strategy.py mj/fit.py mj/bot.py; do
    case "$f" in *.json) add_file "$f" json ;; *) add_file "$f" python ;; esac
done

section "D. 实验汇总"
add_file docs/SIM_FOUNDATION_REPORT.md markdown
for f in reports/phase2_summary.txt reports/phase3_summary.txt reports/s2_summary.txt reports/s3_summary.txt reports/gap_breakdown_detail.txt; do
    add_file "$f" text
done
add_file models/mc_opp_params.json json

section "E. 早期离线分析报告"
add_file docs/experiments/OFFLINE_REPORT.md markdown

section "F. 数据格式样例（截断）"
python3 - >> "$OUT" <<'PY'
import glob, json
files = sorted(glob.glob("models/events/*.json"))
for path in files[-3:]:
    try:
        g = json.load(open(path, encoding="utf-8"))
    except Exception:
        continue
    blocks = g.get("blocks") or []
    if blocks:
        b = dict(blocks[0]); b["events"] = (b.get("events") or [])[:40]
        g = {k: v for k, v in g.items() if k != "blocks"}
        g["blocks"] = [b, "...(其余 block 省略)"]
    g["rounds"] = (g.get("rounds") or [])[:2]
    print("\n## 对局事件文件样例：`%s`（每局 block 只保留前 40 个事件）\n\n```json" % path)
    print(json.dumps(g, ensure_ascii=False, indent=1)[:12000])
    print("```")
    break
for path in sorted(glob.glob("logs/*.jsonl"))[-1:]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"kind": "decision"' in line:
                d = json.loads(line)
                print("\n## 线上决策日志样例（一条 decision 记录）\n\n```json")
                print(json.dumps(d, ensure_ascii=False, indent=1)[:6000])
                print("```")
                break
PY

# 安全检查：资料包里不能出现令牌/cookie
# 变量名 MJ_TOKEN 本身会出现在代码/文档里，不算泄露；只拦真实凭据：Bearer 值，以及令牌/cookie 文件的内容。
if grep -Eq 'Bearer [A-Za-z0-9._-]{20,}' "$OUT"; then
    echo "!!! 资料包里疑似含令牌/cookie 内容，已中止，请检查 $OUT"; exit 1
fi
for t in .mj_token .mj_token_global portal_cookie.txt; do
    if [ -f "$t" ] && [ -s "$t" ] && grep -qF "$(head -c 40 "$t")" "$OUT"; then
        echo "!!! 资料包里出现了 $t 的内容，已删除资料包"; rm -f "$OUT"; exit 1
    fi
done
SIZE=$(wc -c < "$OUT")
echo "已生成 $OUT（$((SIZE / 1024)) KB，约 $((SIZE / 4000)) 千 token 量级；令牌检查通过）"
