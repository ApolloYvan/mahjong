"""S2：网络策略 -> 可交给服务端的动作（实战 + arena 用）。纯标准库。

``nn_action(snapshot, ctx)`` 返回 ``(action_or_None, log)``。``None`` = 回落生产策略（模型文件缺失/加载失败、
推理异常、超过 ``budget_s``、没有合法动作、网络选的动作不在合法集合内……一律回落，原因写进 ``log["fallback"]``）。
动作是否真的合法（有财必靠、墙尾禁杠等规则开关）由调用方 ``mj.bot`` 再用生产自己的判定校验一遍。

决策方式：
- 摸牌阶段、手牌没胡：D 头（34 个出牌 + 自己杠）在合法集合上取 argmax；
- 摸牌阶段、胡牌态（``A_HU`` 合法）：H 头三选一——胡 / 飘（打财神）/ 弃胡（再用 D 头在非财神的出牌里选打哪张）；
- 响应阶段：R 头在合法集合上取 argmax。
"""
import os
import threading
import time

from ..tiles import JOKER, TILE_INDEX
from . import encode as E
from .infer import load

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(ROOT, "models", "nn_policy.json")
BUDGET_S = 0.2

_LOCK = threading.Lock()
_CACHE = {"path": None, "mtime": None, "net": None, "error": None}

D_GANG = 34   # D 头 35 个输出：0..33 出牌，34 自己杠
# R 头下标 -> 统一动作下标
R_TO_ACTION = [E.A_PASS, E.A_PENG, E.A_GANG_MING, E.A_CHI_LOW, E.A_CHI_MID, E.A_CHI_HIGH]


def model_path():
    return os.environ.get("MJ_NN_POLICY_PATH") or DEFAULT_PATH


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
            net, err = load(path), None
        except Exception as exc:   # noqa: BLE001
            net, err = None, "load_error:%r" % (exc,)
        c.update(path=path, mtime=mtime, net=net, error=err)
        return net, err


def _argmax(logits, allowed):
    best, bv = None, None
    for i in allowed:
        if bv is None or logits[i] > bv:
            best, bv = i, logits[i]
    return best


def nn_action(snapshot, ctx=None, budget_s=BUDGET_S, net=None):
    t0 = time.perf_counter()
    log = {}
    if net is None:
        net, err = get_net()
        if net is None:
            log["fallback"] = err
            return None, log
    try:
        legal = E.legal_actions(snapshot)
        if not legal:
            log["fallback"] = "no_legal"
            return None, log
        idx, val = E.encode_sparse(snapshot, ctx)
        out = net.forward(idx, val)
        phase = snapshot.get("phase")
        if phase == "draw":
            discards = [a for a in legal if a < 34]
            d_allowed = discards + ([D_GANG] if E.A_GANG_SELF in legal else [])
            if E.A_HU in legal:
                k = _argmax(out["h"], (0, 1, 2))
                log["head"] = "h"
                if k == 0:
                    a = E.A_HU
                elif k == 1 and E.TILE_INDEX[JOKER] in discards:
                    a = TILE_INDEX[JOKER]
                else:
                    non_joker = [x for x in discards if x != TILE_INDEX[JOKER]]
                    a = _argmax(out["d"], non_joker or discards)
            else:
                log["head"] = "d"
                a = _argmax(out["d"], d_allowed)
                if a == D_GANG:
                    a = E.A_GANG_SELF
        else:
            log["head"] = "r"
            allowed = [i for i, act in enumerate(R_TO_ACTION) if act in legal]
            k = _argmax(out["r"], allowed)
            a = R_TO_ACTION[k] if k is not None else None
        action = E.index_to_action(snapshot, a) if a is not None else None
        log["value"] = round(out["v"], 3)
    except Exception as exc:   # noqa: BLE001 — 任何异常都回落生产
        log["fallback"] = "exception:%r" % (exc,)
        return None, log
    log["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    if action is None:
        log["fallback"] = "no_action"
        return None, log
    if time.perf_counter() - t0 > budget_s:
        log["fallback"] = "timeout"
        return None, log
    return action, log
