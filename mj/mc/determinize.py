"""阶段二 §3：按快照里能看到的信息，把对手的暗牌和牌墙随机补全成一副
完整的、状态一致的 136 张牌。第一版均匀随机，不做读牌（预留钩子，见
``opponent_weight_hook``，本轮不启用——传非 None 会直接 ``NotImplementedError``，
不写一段没有真实依据、没测过的"假读牌"逻辑）。
"""
import random

from ..tiles import NSUITS, counts_to_tiles, visible_counts

DEAL_SIZE = 13


def hand_size(seat, snapshot):
    """摸牌前 13 张、副露每组 -3；当前 turn 座位在 draw 阶段多算 1 张
    （摸到的那张还没打出去）——跟 ``mj/bot.py::_expected_hand_size`` /
    ``mj/state.py`` 同一个口径，不是另起一套。"""
    melds = (snapshot.get("melds") or [[], [], [], []])[seat]
    n = DEAL_SIZE - 3 * len(melds)
    if snapshot.get("turn") == seat and snapshot.get("phase") == "draw":
        n += 1
    return max(0, n)


def unseen_counts(snapshot):
    """34 维未见张数：4 - 我方手牌 - 全部弃牌 - 全部副露里露出的牌
    （``mj.tiles.visible_counts`` 已经是这个口径，直接用，不重复实现）。"""
    visible = visible_counts(snapshot.get("my_hand") or [], snapshot.get("discards"), snapshot.get("melds"))
    return [4 - visible[i] for i in range(NSUITS)]


def determinize(snapshot, rng=None, opponent_weight_hook=None):
    """返回 ``{seat: [tiles...] for seat != 自己} | {"wall": [tiles...]}``。

    张数分配：自己以外 3 个座位按 ``hand_size()`` 各分一手，牌墙拿**正好**
    ``wall_remaining`` 张（服务端口径），不多不少。对自洽的真实快照，
    "未见牌总数" 应该恰好等于 "三个对手手牌 + wall_remaining"（见
    ``mj/bot.py``/``mj/state.py`` 里同一个 ``13-3*meld_groups`` 手牌张数
    公式——这是已经在生产环境验证过的口径，不是这里现推的）；如果真的对
    不上（快照口径不一致的边界情况），宁可缺一点（牌墙张数不足就只给
    能给的，绝不超发）也不把多出来的牌硬塞进牌墙把它"藏起来"——之前的
    实现会把多余的牌一股脑塞进牌墙尾部，表面上不抛异常，实际是在真实
    数据出问题时悄悄伪造一个张数对不上的牌墙，比抛异常更难发现，已经
    被 ``tests/test_mc_determinize.py`` 的张数断言抓出来过一次，不再
    这样做。
    """
    if opponent_weight_hook is not None:
        raise NotImplementedError("读牌钩子预留接口，本轮未启用，见模块 docstring")
    rng = rng or random.Random()
    pool = counts_to_tiles(unseen_counts(snapshot))
    rng.shuffle(pool)

    seat = snapshot["seat"]
    result = {}
    idx = 0
    for s in range(4):
        if s == seat:
            continue
        n = min(hand_size(s, snapshot), len(pool) - idx)
        result[s] = pool[idx:idx + n]
        idx += n
    wall_n = min(snapshot.get("wall_remaining") or 0, max(0, len(pool) - idx))
    result["wall"] = pool[idx:idx + wall_n]
    return result


def determinize_batch(snapshot, n, seed):
    """CRN（共用随机牌局）：给定 ``seed``，生成 ``n`` 组确定性的补全牌局
    ——同一个 ``(snapshot, seed)`` 永远生成同一批牌局，供 ``decide.py``
    里所有候选动作在同一批牌局上评估（配对比较，降方差），也供 arena
    模式按"对局种子+决策序号"复现结果。"""
    rng = random.Random(seed)
    return [determinize(snapshot, rng=rng) for _ in range(n)]
