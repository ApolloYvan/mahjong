"""S2'：逐候选"相对生产的优势"模型（纯 Python 推理）。

    adv_i = g(x_i)        i>=1（x_i 是生产排名第 i 的候选的特征，``mj.nn.cands``）；adv_0 = 0（生产选择 P 自己）
    g(x) = W3 relu(W2 relu(W1 (x-mean)/std + b1) + b2) + b3        W3、b3 初始为 0（训练代码保证）

模型直接预测"打这张牌比打生产选择平均多/少几分（分/局）"（训练目标来自 tools/train/search_s3.py 的检验批配对差），
不在生产评分上叠加修正。决策：``max_i>=1 adv_i > tau`` 才改动，否则沿用生产——未训练（g≡0）时严格等于生产策略；
``tau``（分/局）在验证集上调、存在权重文件 ``meta`` 里。权重文件（``models/nn_resid.json``）：``mj.nn.infer.pack`` 格式，
``meta`` 带 ``mean/std/tau/K/feature_dim``；张量 w1[D,h1] b1 w2[h1,h2] b2 w3[h2,1] b3。
``rescore`` 只处理"摸牌后出牌、生产选了弃牌、没有抓打圈限制"的决策，其它一律返回 None（沿用生产）；
特征提取/推理超时或任何异常 -> None。
"""
import array
import json
import os
import threading
import time

from . import cands as C
from .infer import _unb64

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(ROOT, "models", "nn_resid.json")
BUDGET_S = 0.25
FEATURE_BUDGET_S = 0.05      # 特征提取的时间上限：到点就只用已经算好的候选（至少要有 P 之外的 1 个）
_LOCK = threading.Lock()
_CACHE = {"path": None, "mtime": None, "net": None, "error": None}


class ResidNet:
    def __init__(self, obj):
        meta = obj.get("meta") or {}
        self.dim = obj["feature_dim"]
        if self.dim != C.FEATURE_DIM:
            raise ValueError("特征维度不一致：模型 %d，代码 %d" % (self.dim, C.FEATURE_DIM))
        self.mean, self.std = meta["mean"], meta["std"]
        self.tau = float(meta.get("tau", 1.0))
        self.k = int(meta.get("K", C.K_DEFAULT))
        h1, h2 = obj["hidden"]
        t = {k: _unb64(v) for k, v in obj["tensors"].items()}

        def rows(name, nr, nc):
            flat = t[name]
            return [list(flat[i * nc:(i + 1) * nc]) for i in range(nr)]
        self.w1, self.b1 = rows("w1", self.dim, h1), list(t["b1"])
        self.w2, self.b2 = rows("w2", h1, h2), list(t["b2"])
        self.w3, self.b3 = [r[0] for r in rows("w3", h2, 1)], t["b3"][0]

    def g(self, x):
        xs = [(v - m) / s for v, m, s in zip(x, self.mean, self.std)]
        acc = self.b1[:]
        for i, v in enumerate(xs):
            if v:
                acc = [a + v * w for a, w in zip(acc, self.w1[i])]
        acc2 = self.b2[:]
        for i, a in enumerate(acc):
            if a > 0.0:
                acc2 = [b + a * w for b, w in zip(acc2, self.w2[i])]
        return self.b3 + sum(a * w for a, w in zip(acc2, self.w3) if a > 0.0)

    def advantages(self, vectors):
        """vectors[0] 是生产选择 P：优势恒为 0；其余候选 g(x)。"""
        return [0.0] + [self.g(x) for x in vectors[1:]]


def pack_resid(hidden, tensors, mean, std, tau=1.0, k=C.K_DEFAULT, extra=None):
    """tensors: {w1,b1,w2,b2,w3,b3: (形状, 展平 float 列表)} -> 权重文件 dict（训练代码/测试用）。"""
    from .infer import pack
    meta = {"mean": list(mean), "std": list(std), "tau": tau, "K": k, "names": list(C.FEATURE_NAMES), "mode": "adv"}
    meta.update(extra or {})
    return pack(C.FEATURE_DIM, hidden, tensors, meta)


def model_path():
    return os.environ.get("MJ_NN_RESID_PATH") or DEFAULT_PATH


def get_net():
    path = model_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, "no_model"
    with _LOCK:
        c = _CACHE
        if c["path"] == path and c["mtime"] == mtime:
            return c["net"], c["error"]
        try:
            with open(path, encoding="utf-8") as f:
                net, err = ResidNet(json.load(f)), None
        except Exception as exc:   # noqa: BLE001
            net, err = None, "load_error:%r" % (exc,)
        c.update(path=path, mtime=mtime, net=net, error=err)
        return net, err


def rescore(snapshot, prod_action, ctx=None, rules=None, net=None, budget_s=BUDGET_S):
    """返回 (新动作或 None, log)。None = 沿用生产。"""
    t0 = time.perf_counter()
    log = {}
    if not prod_action or prod_action.get("action") != "discard" or snapshot.get("phase") != "draw":
        return None, log
    god = snapshot.get("god") or {}
    if god.get("catch_play") and god.get("god_discarder_seat") != snapshot.get("seat"):
        return None, log            # 抓打圈受限：只能打刚摸到的牌
    if net is None:
        net, err = get_net()
        if net is None:
            log["fallback"] = err
            return None, log
    try:
        cs = C.production_candidates(snapshot, prod_tile=prod_action.get("tile"), k=net.k, rules=rules)
        if len(cs) < 2:
            return None, log
        vecs = C.candidate_features(snapshot, cs, ctx, rules, skip_prod=True, deadline=t0 + FEATURE_BUDGET_S)
        cs = cs[:len(vecs)]
        if len(vecs) < 2:
            log["fallback"] = "features_timeout"
            return None, log
        adv = net.advantages(vecs)
        best = max(range(len(cs)), key=lambda i: adv[i])
        if adv[best] <= net.tau:
            best = 0                       # 没有候选的预测优势超过阈值：沿用生产
        log.update({"cands": [c["tile"] for c in cs], "adv": [round(x, 3) for x in adv], "pick": best})
    except Exception as exc:   # noqa: BLE001
        log["fallback"] = "exception:%r" % (exc,)
        return None, log
    log["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    if time.perf_counter() - t0 > budget_s:
        log["fallback"] = "timeout"
        return None, log
    if best == 0:
        return None, log
    return {"action": "discard", "tile": cs[best]["tile"]}, log
