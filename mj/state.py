"""统一决策状态：DecisionState 是单一真相，规范化读取原始快照字段。

背景（已在独立技术评审报告与官网规则核验中确认，非本次臆测）：
- 官网规范字段是 ``god.chain_count`` / ``god.piao``；历史代码在 ``mj/bot.py`` 的
  ``hu_result``/``can_hu``/``choose_discard`` 中读取顶层 ``snapshot["chain_count"]``，
  而该顶层字段历史上从未被服务端真正写入（评审报告：128 条 ``god.chain_count>0``
  的决策样本中，无一条保存了顶层 ``chain_count``）。这直接导致链/飘方向策略在线上
  从未真正按非零链次数评分过。
- 20 张墙尾（``wall_remaining<=20``）禁止杠必须适用于全部杠路径；历史上
  ``bot._gang_bomb_choice`` 使用了错误的 ``<=4`` 阈值，绕开了这条规则。

DecisionState 本身不改变任何策略评分函数，只负责把原始 snapshot dict 转成
规范化、一致的只读视图，供 legacy 策略与未来规划器共用一套读取口径。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def melds_for_seat(snapshot: dict, seat) -> list:
    """兼容两种快照形态：4 座位列表 或 {seat: groups} 字典。"""
    melds = snapshot.get("melds") or {}
    if isinstance(melds, list):
        if len(melds) == 4 and all(isinstance(m, list) for m in melds):
            if isinstance(seat, int) and 0 <= seat < 4:
                return melds[seat]
            return []
        return melds
    return melds.get(str(seat), melds.get(seat, []))


def meld_count(snapshot: dict, seat) -> int:
    return len(melds_for_seat(snapshot, seat))


def canonical_chain_count(snapshot: dict) -> int:
    """规范链次数：``god.chain_count`` 优先；仅当 god 字典缺失该键时才回退到
    顶层 ``chain_count``（兼容早期未迁移的测试夹具/历史日志）。两者皆缺失为 0。
    """
    god = snapshot.get("god")
    if isinstance(god, dict) and "chain_count" in god:
        return god.get("chain_count") or 0
    return snapshot.get("chain_count", 0) or 0


def canonical_piao(snapshot: dict) -> int:
    """规范财飘计数：``god.piao`` 优先，回退顶层 ``piao``，语义同上。"""
    god = snapshot.get("god")
    if isinstance(god, dict) and "piao" in god:
        return god.get("piao") or 0
    return snapshot.get("piao", 0) or 0


@dataclass
class DecisionState:
    """规范化决策状态：单一真相。字段命名保持与原始快照语义一致，
    但读取路径已修正为官网核验后的规范字段。"""

    raw: Dict[str, Any]
    seat: int
    phase: Optional[str]
    turn: Optional[int]
    hand: List[str]
    drawn_tile: Optional[str]
    dealer: Optional[int]
    wall_remaining: int
    discards: Any
    melds_all: Any
    responding_seats: List[int]
    god: Dict[str, Any]
    chain_count: int
    piao: int
    rules: Dict[str, Any]
    round_no: Any
    tournament_id: Optional[str] = None
    catch_play: bool = field(init=False, default=False)
    god_discarder_seat: Optional[int] = field(init=False, default=None)

    def __post_init__(self):
        self.catch_play = bool(self.god.get("catch_play"))
        self.god_discarder_seat = self.god.get("god_discarder_seat")

    @property
    def meld_groups(self) -> int:
        return len(self.melds_for(self.seat))

    def melds_for(self, seat) -> list:
        return melds_for_seat(self.raw, seat)

    def is_dealer(self) -> bool:
        return self.dealer is not None and self.dealer == self.seat

    def catch_restricted(self) -> bool:
        """抓打圈：打出财神那一圈，其余玩家（非打财神本人）只能打刚摸到的牌。"""
        return self.catch_play and self.god_discarder_seat != self.seat

    def wall_tail(self, threshold: int = 20) -> bool:
        """20 张墙尾（含）以内：全部杠路径均禁止杠，摸完流局。"""
        return self.wall_remaining is not None and self.wall_remaining <= threshold

    def hand_expectation(self, seat=None) -> int:
        """摸牌前手牌应有张数 = 13 - 3*副露组数。"""
        seat = self.seat if seat is None else seat
        return 13 - 3 * self.meld_count_for(seat)

    def meld_count_for(self, seat) -> int:
        return len(self.melds_for(seat))


_REPLAY_FIELDS = (
    "seat", "phase", "turn", "hand", "drawn_tile", "dealer", "wall_remaining",
    "discards", "melds_all", "responding_seats", "chain_count", "piao", "round_no",
)


def replay_dict(state: "DecisionState") -> Dict[str, Any]:
    """B3 返修：state_sample/anomaly_window 需要保存可复盘的规范化状态，而
    不只是 hash/phase/seat 等元数据。只取与 ``mj.observability`` 的
    ``state_hash`` 计算同一口径的有界字段，不保存原始 snapshot 里无关的
    （每次轮询都会变化但与决策无关的）字段，避免日志体积无界膨胀。"""
    return {name: getattr(state, name) for name in _REPLAY_FIELDS}


def normalize(snapshot: dict, rules: Optional[dict] = None,
              tournament_id: Optional[str] = None) -> Optional[DecisionState]:
    """把原始 /state 快照 dict 转成 DecisionState。snapshot 为空返回 None。"""
    if not snapshot:
        return None
    seat = snapshot.get("seat", -1)
    return DecisionState(
        raw=snapshot,
        seat=seat,
        phase=snapshot.get("phase"),
        turn=snapshot.get("turn"),
        hand=list(snapshot.get("my_hand") or []),
        drawn_tile=snapshot.get("drawn_tile"),
        dealer=snapshot.get("dealer"),
        wall_remaining=snapshot.get("wall_remaining", 99),
        discards=snapshot.get("discards") or [],
        melds_all=snapshot.get("melds") or [],
        responding_seats=snapshot.get("responding_seats") or [],
        god=snapshot.get("god") or {},
        chain_count=canonical_chain_count(snapshot),
        piao=canonical_piao(snapshot),
        rules=rules or snapshot.get("rules") or {},
        round_no=snapshot.get("round_no"),
        tournament_id=tournament_id,
    )
