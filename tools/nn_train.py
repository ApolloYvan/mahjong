"""训练弃牌小模型（需要 torch，只在训练时用；线上推理是 mj/nn_discard.py 的纯 Python）。

    pip3 install torch
    python3 tools/nn_train.py                         # 读 data/analysis/nn_discard.jsonl，写 models/nn_discard.json
    python3 tools/nn_train.py --hidden 64 32 --epochs 40

目标与线性拟合相同（条件 logit）：每个决策点的候选打分做 softmax，让高手实际打的那张概率最大。
样本按高手「扣掉牌运后的分/局」加权（tools/luck_vs_masters.py 的结果；--uniform 则等权）。
训练/测试按对局文件切分（nn_extract 里的 t 字段，与 discard_fit 同一切分），早停看测试集损失。
报告：测试集上 模型 vs 现行策略 的一致率（总、有无财神、每位高手），并核对纯 Python 前向与 torch 一致。
只有模型一致率高于现行时才写出模型文件（--force 强制写）。
"""
import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mj.nn_discard import FEATURES, forward  # noqa: E402

# tools/luck_vs_masters.py（2026-09-27）实际-期望 分/局；权重 = 0.5 + max(0, 值)
SKILL = {"歪比巴卜肉蛋葱鸡": 2.05, "⭐꧁༺🀆🀆🀆🀆༻꧂⭐": 1.95, "Astra-0": 1.43, "爆头研究所": 1.05,
         "放假了偷偷训练": 1.00, "晴总总，该请桂语山房了": 0.97, "Deepseek胡": 0.72, "铳一色14": 0.51,
         "康陶应雀": 0.49, "glm-flash": 0.01}


def load(path):
    rows = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            rows.append(json.loads(line))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inp", default="data/analysis/nn_discard.jsonl")
    ap.add_argument("--out", default="models/nn_discard.json")
    ap.add_argument("--hidden", type=int, nargs="+", default=[64, 32])
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--wd", type=float, default=1e-4)
    ap.add_argument("--patience", type=int, default=5)
    ap.add_argument("--uniform", action="store_true", help="高手样本等权")
    ap.add_argument("--players", nargs="*", help="只用这几位高手训练（早停、是否写出也只看他们）；缺省 = 全部 10 位")
    ap.add_argument("--force", action="store_true", help="不如现行也写出模型文件")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    import torch
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    torch.set_num_threads(max(1, (os.cpu_count() or 4) - 2))

    rows = load(args.inp)
    D = len(FEATURES)
    if not rows or len(rows[0]["X"][0]) != D:
        sys.exit("特征维度对不上：数据 %s 维，mj/nn_discard.FEATURES %d 维——请重新跑 tools/nn_extract.py"
                 % (len(rows[0]["X"][0]) if rows else 0, D))
    target = set(args.players or SKILL)
    train = [r for r in rows if not r["t"] and r["p"] in target]
    test_all = [r for r in rows if r["t"]]
    test = [r for r in test_all if r["p"] in target]
    print("训练对象：%s" % ("全部 10 位高手" if not args.players else "、".join(args.players)))
    print("决策点：训练 %d / 测试 %d（全部高手测试 %d）；每个候选 %d 维" % (len(train), len(test), len(test_all), D))

    # 标准化（只用训练集）
    s1, s2, n = [0.0] * D, [0.0] * D, 0
    for r in train:
        for x in r["X"]:
            n += 1
            for i, v in enumerate(x):
                s1[i] += v
                s2[i] += v * v
    mean = [a / n for a in s1]
    std = [math.sqrt(max(b / n - m * m, 0.0)) or 1.0 for b, m in zip(s2, mean)]

    C = max(len(r["X"]) for r in rows)

    def tensors(rs):
        X = torch.zeros(len(rs), C, D)
        M = torch.zeros(len(rs), C, dtype=torch.bool)
        y = torch.zeros(len(rs), dtype=torch.long)
        w = torch.ones(len(rs))
        for k, r in enumerate(rs):
            xs = torch.tensor(r["X"], dtype=torch.float32)
            X[k, :len(r["X"])] = xs
            M[k, :len(r["X"])] = True
            y[k] = r["y"]
            if not args.uniform:
                w[k] = 0.5 + max(0.0, SKILL.get(r["p"], 0.5))
        X = (X - torch.tensor(mean)) / torch.tensor(std)
        return X, M, y, w

    Xtr, Mtr, ytr, wtr = tensors(train)
    Xte, Mte, yte, wte = tensors(test)

    layers, prev = [], D
    for h in args.hidden:
        layers += [torch.nn.Linear(prev, h), torch.nn.ReLU()]
        prev = h
    layers.append(torch.nn.Linear(prev, 1))
    net = torch.nn.Sequential(*layers)
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=args.wd)

    def logits(X, M):
        s = net(X).squeeze(-1)
        return s.masked_fill(~M, -1e9)

    def evaluate(X, M, y, w):
        with torch.no_grad():
            lg = logits(X, M)
            loss = (torch.nn.functional.cross_entropy(lg, y, reduction="none") * w).sum() / w.sum()
            return float(loss), lg.argmax(-1)

    best_loss, best_state, bad = math.inf, None, 0
    for ep in range(1, args.epochs + 1):
        net.train()
        perm = torch.randperm(len(train))
        tot = 0.0
        for i in range(0, len(train), args.batch):
            idx = perm[i:i + args.batch]
            lg = logits(Xtr[idx], Mtr[idx])
            loss = (torch.nn.functional.cross_entropy(lg, ytr[idx], reduction="none") * wtr[idx]).sum() / wtr[idx].sum()
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss.detach()) * len(idx)
        net.eval()
        te_loss, pred = evaluate(Xte, Mte, yte, wte)
        acc = float((pred == yte).float().mean())
        print("  第 %2d 轮  训练损失 %.4f  测试损失 %.4f  测试一致率 %.1f%%" % (ep, tot / len(train), te_loss, 100 * acc),
              flush=True)
        if te_loss < best_loss - 1e-4:
            best_loss, bad = te_loss, 0
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
        else:
            bad += 1
            if bad >= args.patience:
                print("  测试损失 %d 轮没有下降，停止" % args.patience)
                break
    net.load_state_dict(best_state)
    net.eval()
    Xa, Ma, ya, wa = tensors(test_all)
    _, pred = evaluate(Xa, Ma, ya, wa)
    pred = pred.tolist()

    # 报告：同一批测试决策上 模型 vs 现行；「全部/有无财神/庄闲」只算训练对象，每人一行全列
    agg = defaultdict(lambda: [0, 0, 0])      # key -> [n, 模型对, 现行对]
    for r, p in zip(test_all, pred):
        if r["cur"] < 0:
            continue
        keys = ["p:" + r["p"]]
        if r["p"] in target:
            keys += ["全部", "有财神" if r["j"] else "无财神", "庄" if r["d"] else "闲"]
        for k in keys:
            a = agg[k]
            a[0] += 1
            a[1] += p == r["y"]
            a[2] += r["cur"] == r["y"]
    print("\n=== 测试集（高手真实决策）一致率：现行策略 → 小模型（全部/有无财神/庄闲 只算训练对象）===")
    for k in ["全部", "无财神", "有财神", "庄", "闲"] + sorted((k for k in agg if k.startswith("p:")),
                                                        key=lambda k: -agg[k][0]):
        n, m, c = agg[k]
        if n:
            print("  %-26s n=%6d   %5.1f%% → %5.1f%%   (%+.1f)" % (k.replace("p:", "  "), n, 100 * c / n, 100 * m / n,
                                                                100 * (m - c) / n))

    # 导出 + 纯 Python 前向核对
    lin = [m for m in net if isinstance(m, torch.nn.Linear)]
    model = {"features": list(FEATURES), "mean": mean, "std": std,
             "layers": [{"W": m.weight.detach().tolist(), "b": m.bias.detach().tolist()} for m in lin],
             "hidden": args.hidden, "source": os.path.basename(args.inp)}
    diff = 0.0
    with torch.no_grad():
        for r in test[:200]:
            for x in r["X"]:
                t = float(net((torch.tensor([x]) - torch.tensor(mean)) / torch.tensor(std)))
                diff = max(diff, abs(t - forward(model, x)))
    print("\n纯 Python 前向 vs torch 最大误差：%.2e（应 < 1e-3）" % diff)
    n, m, c = agg["全部"]
    model["test"] = {"n": n, "model_acc": m / n if n else 0, "cur_acc": c / n if n else 0}
    if diff > 1e-3:
        sys.exit("前向不一致，不写模型文件")
    if m <= c and not args.force:
        print("小模型不比现行好（%.1f%% vs %.1f%%），不写模型文件。" % (100 * m / n, 100 * c / n))
        return
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as sink:
        json.dump(model, sink)
    os.replace(tmp, args.out)       # 原子替换：线上 bot 不会读到写了一半的文件
    print("已写出 %s（%.0f KB）。线上启用：weights.json 加 \"rule_nn_discard_enabled\": 1（需重启一次以加载新代码）。"
          % (args.out, os.path.getsize(args.out) / 1024))


if __name__ == "__main__":
    main()
