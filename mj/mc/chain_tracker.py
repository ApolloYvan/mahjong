"""按局记录 MC 需要、但服务端快照里没有的两个量（见 ``mj/mc/decide.py`` docstring）：

- ``piao_count``：本座位**当前这条链**里自由飘出（自由弃财神）的次数，喂给"4个白板"计番
  （手里剩的财神数 + piao_count >= 4）。规则同 ``mj.sim.engine``：自由弃财神 +1；本座位自己
  的一次普通弃牌让（本座位作为 chain_owner 的）链清零；链在快照里看不到了（chain_count=0）也清零。
- ``chain_has_gang``：当前链里是否含杠（含杠的链兑现不要求爆头）。本座位杠一次置 True；
  链清零时复位。

只靠本座位自己的动作 + 快照里按座位下发的 ``god.chain_count`` 推出来，不读任何服务端没给我们
的信息。bot 每次决策前调 ``ctx(snapshot)``，动作被服务端接受之后调 ``observe(snapshot, action)``。
"""


class ChainTracker:
    def __init__(self):
        self.round_no = None
        self.piao = 0
        self.has_gang = False

    def _sync(self, snapshot):
        rn = snapshot.get("round_no")
        if rn != self.round_no:
            self.round_no = rn
            self.piao = 0
            self.has_gang = False
        god = snapshot.get("god") or {}
        if not (god.get("chain_count") or 0):
            self.piao = 0
            self.has_gang = False

    def ctx(self, snapshot):
        self._sync(snapshot)
        return {"piao_count": self.piao, "chain_has_gang": self.has_gang}

    def observe(self, snapshot, action):
        if snapshot.get("phase") != "draw" or not action:
            return
        self._sync(snapshot)
        kind = action.get("action")
        seat = snapshot.get("seat")
        god = snapshot.get("god") or {}
        if kind == "discard":
            restricted = bool(god.get("catch_play")) and god.get("god_discarder_seat") != seat
            if action.get("tile") == "白" and not restricted:
                self.piao += 1
            else:
                self.piao = 0
        elif kind == "gang":
            if not (god.get("chain_count") or 0):
                self.piao = 0   # 别人的链被我们的杠接管，旧链作废
            self.has_gang = True
