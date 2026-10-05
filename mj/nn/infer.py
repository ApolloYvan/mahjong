"""S2：纯标准库的网络前向（实战用）+ 权重文件读写。

网络（训练代码在 tools/train/，结构必须一致）::

    x(稀疏, FEATURE_DIM) --EmbeddingBag/W1+b1--> ReLU --W2+b2--> ReLU(h) --> 头
    头：D 出牌/自己杠 35 个 logit；R 响应 6 个 logit（过/碰/明杠/吃低/吃中/吃高）；
        H 胡牌态 3 个 logit（胡/飘/弃胡）；V 价值 1 个标量。

权重文件 JSON：``{"version": 1, "feature_dim": ..., "hidden": [h1, h2], "tensors": {名字: base64(float32 小端)},
"shapes": {名字: [..]}, "meta": {...}}``，名字 w1[F,h1] b1 w2[h1,h2] b2 wd[h2,35] bd wr[h2,6] br wh[h2,3] bh wv[h2,1] bv。
权重一律按"输入维在前"（行 = 输入单元）存，前向对输入的非零项累加整行——稀疏第一层和 ReLU 后的稀疏都能省掉
大部分乘加。float32 解码成 Python float（float64）再算，跟 torch 的 float64 前向（同一份被舍入后的权重）
的差在 1e-12 量级，验收线 1e-5（tools/train/verify_infer.py）。
"""
import array
import base64
import json
import sys

HEAD_SIZES = {"d": 35, "r": 6, "h": 3, "v": 1}
VERSION = 1


def _b64_floats(values):
    a = array.array("f", values)
    if sys.byteorder == "big":
        a.byteswap()
    return base64.b64encode(a.tobytes()).decode("ascii")


def _unb64(text):
    a = array.array("f")
    a.frombytes(base64.b64decode(text))
    if sys.byteorder == "big":
        a.byteswap()
    return a


def pack(feature_dim, hidden, tensors, meta=None):
    """tensors: {名字: (形状, 展平的 float 列表)} -> 可 json.dump 的 dict。"""
    return {"version": VERSION, "feature_dim": feature_dim, "hidden": list(hidden),
            "shapes": {k: list(v[0]) for k, v in tensors.items()},
            "tensors": {k: _b64_floats(v[1]) for k, v in tensors.items()}, "meta": meta or {}}


class Net:
    def __init__(self, obj):
        if obj.get("version") != VERSION:
            raise ValueError("权重文件版本不对")
        self.feature_dim = obj["feature_dim"]
        h1, h2 = obj["hidden"]
        self.h1, self.h2 = h1, h2
        self.meta = obj.get("meta") or {}
        t = {k: _unb64(v) for k, v in obj["tensors"].items()}

        def rows(name, n_rows, n_cols):
            flat = t[name]
            if len(flat) != n_rows * n_cols:
                raise ValueError("张量 %s 大小不对" % name)
            return [list(flat[i * n_cols:(i + 1) * n_cols]) for i in range(n_rows)]

        self.w1 = rows("w1", self.feature_dim, h1)
        self.b1 = list(t["b1"])
        self.w2 = rows("w2", h1, h2)
        self.b2 = list(t["b2"])
        self.heads = {}
        for key, n in HEAD_SIZES.items():
            self.heads[key] = (rows("w" + key, h2, n), list(t["b" + key]))

    def forward(self, indices, values):
        """稀疏输入 -> {"d": [...], "r": [...], "h": [...], "v": float}（未加掩码的原始 logit）。"""
        w1 = self.w1
        acc = self.b1[:]
        for i, v in zip(indices, values):
            acc = [a + v * w for a, w in zip(acc, w1[i])]
        w2 = self.w2
        acc2 = self.b2[:]
        for i, a in enumerate(acc):
            if a > 0.0:
                acc2 = [b + a * w for b, w in zip(acc2, w2[i])]
        hid = [x if x > 0.0 else 0.0 for x in acc2]
        out = {}
        for key, (w, b) in self.heads.items():
            res = b[:]
            for i, x in enumerate(hid):
                if x:
                    res = [r + x * ww for r, ww in zip(res, w[i])]
            out[key] = res
        out["v"] = out["v"][0]
        return out


def load(path):
    with open(path, encoding="utf-8") as f:
        return Net(json.load(f))
