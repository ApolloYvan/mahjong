"""G1：与服务端逐事件核对的麻将规则状态机（docs/SIM_FOUNDATION_REPORT.md）。

纯标准库，import 时无 I/O，不 import mj.arena（旧策略评测模块，见
``build_wall`` 的说明）。番数计算直接复用 ``mj.rules.evaluate``，不重写。

======================================================================
实证依据（来自真实日志，样本数如实标注；bot 已停止后用 tools/models/events/
与 logs/*.jsonl 核实，不是凭规则文档猜测。命令见 docs/SIM_FOUNDATION_REPORT.md）
======================================================================
- 杠后补牌：紧跟的 ``tile_drawn`` 事件带 ``data={"gang_replenish": True}``。
- 流局阈值：全语料只有 6 个流局局（0.15%），全部在全桌合计摸牌数
  （含杠后补牌）恰好 63 张、即 wall_remaining=21 时停摸——不是常见假设的
  剩 20 张。n=6，100% 一致。停摸本身不是"尝试摸第 64 张发现没牌"，而是最后
  一次弃牌的响应窗口（碰窗口 + 吃窗口）都无人声明之后直接 round_ended，
  从未真的尝试过那次摸牌。
- 抓打圈 ``catch_play`` 标记语义：对 69296 个真实弃牌事件核对，
  ``data.catch_play`` 反映"应用完这次弃牌之后"的状态（含触发者打财神的
  那一下本身就是 True），解除条件是"轮到触发者自己再次弃牌、且这次不是
  再飘"——不是"数满 3 次别家弃牌"这种计数器口径（两者在没有人中途吃碰
  打断轮转时结果一样，但用轮转对比更贴近服务端实际判据，也天然覆盖
  "触发者中途吃碰打断轮转"的情形）。0 不一致，n=69296。
- 抓打圈响应豁免：只有触发者本人（``god_discarder_seat``）能在抓打圈期间
  吃/碰/杠，经验证 196 次非触发者弃牌无一例外遵守"打刚摸到的那张"，
  45 次触发者自己的弃牌不受此限（可以打任意张）。
- 响应窗口分两段、服务端总是先问全部非打牌方"碰"（不看手牌是否真的够，
  没资格的人也会收到 timeout/pass），再问下家"吃"：抽查 27441 个非抓打圈、
  非财神弃牌，94%（25827 例）在没有人声明时，碰窗口的响应座位集合正好是
  另外 3 家；不足 3 家的情形都对应"某家直接声明碰/杠，其余家的响应因此
  从日志里消失"（一旦有人声明，其余待定响应不会被记录），不是碰窗口本身
  只问了部分人。
- ``chain_count``/``piao``（第二轮返修，15 局残留 fan 不一致逐局查实后改写，
  全部证据来自本节列出的具体局号，样本数如实标注）：
  1. 杠也算一次链上动作，和飘共用同一个 ``chain_count``/``chain_owner``——
     不是两套独立机制。样本：``a_101ea8afdf09_r1_b0_t0`` 第 3 局（同一座位
     连续暗杠+补杠，无中间弃牌，最后一次杠后补牌直接自摸财神，
     detail=['平胡','连杠×2']，fan=4，全程无爆头标签）；
     ``a_9cffd9377e9c_r1_b8_t0`` 第 4 局（暗杠后紧接着飘出杠后补到的财神，
     detail=['平胡','杠飘链×2','爆头']，fan=8）。服务端按"链里的动作构成"
     三选一命名（纯杠→"杠开"/"连杠×N"，纯飘→"财飘"/"双财飘"/"三财飘"，
     混合→"杠飘链×N"），但 fan 倍数统一是 ``2**chain_count``，和旧的
     "gang_open 独立标志"设计只是巧合地在"仅一次杠"时数值相同——两次以上
     的连杠会证伪独立设计（上面第一个样本）。因此引擎不再用
     ``gang_open``/``gang_open_pending`` 这条路（``mj.rules.evaluate()`` 的
     ``gang_open`` 形参永远传 False，不再是"不重写 evaluate()"的例外，
     只是不再使用它）。
  2. 链的番数归属判据是"座位是不是 chain_owner 或者当前
     god_discarder_seat 之一"（取"或"，两个条件缺一都有真实反例）：
     ``a_61537f801625_r1_b0_t0`` 第 7 局证伪"只看 chain_owner"——seat3 自由
     飘白（chain_owner=3），seat0 之后被迫打出自己摸到的财神（豁免转移到
     seat0，chain_owner 不变，且 seat3 后续一次普通弃牌已经按旧规则把
     chain_owner 清零了），seat0 自摸时日志仍给"财飘"（fan=4）——即使
     chain_owner 已清零，当前豁免持有人（seat0，恰好是胡牌人）依然兑现了
     链。``a_25726d650449_r1_b6_t0`` 第 2 局证伪"只看 god_discarder_seat"——
     seat0 自由飘（chain_owner=god_discarder=0），seat3 之后被迫打出摸到的
     财神（豁免转移到 seat3，chain_owner 仍是 0），seat0 自摸时日志仍给
     "财飘"（fan=4）——胡牌人不是当前豁免持有人，但仍是原始 chain_owner，
     照样兑现。两个条件取"或"能同时解释这两局。
  3. 纯飘链（未出现过杠）需要胡牌时刻爆头（听任意牌）才兑现整条链的倍数，
     含杠的链不需要：``a_1b7d9ba3c608_r1_b5_t0`` 第 1 局，胡牌人自己飘白、
     随后立刻自摸，chain_owner=胡牌人本人（归属判据满足），但手牌没到
     爆头，日志是纯"平胡"（fan=1，完全没有财飘倍数，不是打折×1）——说明
     不爆头时整条链都不算，不只是少一个爆头的独立×2。另外三个同构样本
     （``a_737073f58d2a`` 第 5 局、``a_8e3e3e0b75aa`` 第 8 局、
     ``a_b17eb725d9fb`` 第 2 局）未逐条重新核对事件，按同一模式归类。含杠
     的链见上面第 1 点两个样本，均未爆头或爆头独立于链数生效。
  4. "4个白板"（``mj.rules.evaluate()`` 已有对应参数 ``piao``，本轮只是
     发现之前恒传 0 是接错了，不是 evaluate() 缺机制）：判据是"胡牌时手里
     还剩的财神数 + 本座位当前这条链里自由飘出过的财神次数 == 4"。样本
     ``a_3ede5bc381f1_r1_b7_t0`` 第 8 局逐事件核实：起手 3 张财神，本局
     又摸到第 4 张后立即飘掉 1 张（自由选择），胡牌时手里剩 3 张 + 这次
     飘出 1 张 = 4，detail=['平胡','财飘','4个白板','爆头']，fan=8，
     完全对得上 1(平胡)×2(财飘链=1)×2(4个白板)×2(爆头)。``piao_count``
     一开始按"本局历史累计、不随链清零"实现，被全量回放证伪
     （``a_2d09364ee9ca_r1_b3_t0`` 第 1 局：庄家起手 3 张财神，开局直接
     打出 1 张（chain_owner=0），轮到自己又正常弃牌一次、链清零；后面
     又摸到 1 张飘掉（第二条独立的链，chain_owner=0），胡牌时手里剩 2 张。
     如果 piao_count 是历史累计，两次飘加起来是 2，2+2=4 会误判"4个白板"，
     但真实 detail 只有['平胡','爆头']，没有这个标签）——改成跟着链一起
     清零：``chain_owner`` 自己退出这条链时（``_advance_catch_play``）或者
     被别的座位的杠接管时（``_advance_gang_chain``）都把对应座位的
     ``piao_count`` 清成 0，只反映"当前这条还没结清的链"里贡献了几次，
     不是终身累计。全语料"4个白板"出现 28 次（约 0.12%，22751 个非流局
     局），另外几个样本（``a_a53c59e9a271`` 第 4 局、``a_ef40ad5fb5e4``
     第 3 局、``a_b5300d249057`` 第 3 局、``a_2b8650b48048`` 第 2 局）
     未逐条重新核对，按同一判据归类。
  链的上限证据很薄（只观察到 1 次"三财飘"、0 次更高），继续按
  "chain_count>=3 时不能再飘"实现，但只对飘生效，不对杠生效（见
  ``_advance_gang_chain``：没有证据支持杠链也封顶在 3，强行拒绝真实发生过
  的杠反而会把回放搞出新的"非法动作"，弊大于利），如实标注这条只有 n=1
  的支持，不是强证据。
- 庄家起手牌：全语料每一局的庄家 ``start_hands`` 都是 14 张（其余 3 家 13
  张），日志压根不给这张"发牌自带的摸牌"记单独的 ``tile_drawn``——第一条
  事件直接是庄家的弃牌。``WALL_EXHAUST_REMAINING=21`` 这个阈值本来就是按
  "``drawn_count`` 不计这张隐藏摸牌"校准出来的（见上面"流局阈值"的 n=6/
  n=24），``RoundEngine.from_known_hands`` 因此不给这张隐藏摸牌加
  ``drawn_count``；只有极少数局（全语料 n=3）庄家直接靠这张隐藏摸牌自摸、
  事件列表只有一条 ``round_ended``，这种局单独在回放工具里把 ``drawn_tile``
  摆出来（不动 ``drawn_count``），否则 ``apply_hu`` 会因为
  ``drawn_tile is None`` 报错。
- 全量 3298 个文件 / 26086 局强制回放（用户执行）：局末结算五项 +
  3a(catch_play) + 3b(碰/吃窗口) 的不一致数 = 0。返修过程分三轮，每轮都是
  "跑全量→揪出剩余不一致→逐局查实→改代码"，不是一次到位：
  第一轮（加入杠飘统一计链 + chain_owner/god_discarder 取"或"归属 + 纯飘链
  爆头门槛 + piao_count 接 4个白板）跑全量后剩 18 条不一致（6 局），查实后
  发现 4 条子规则仍不完整：(a) chain_owner 自己返回时不能无脑清零
  chain_count——要看 god_discarder_seat 是否还有另一个仍然活跃的持有人
  （``a_61537f801625`` r7）；(b) 不同座位的杠会接管链、不是叠加
  （``a_972a8d0f49ed`` r6）；(c) piao_count 要跟着链一起清零/接管，不是
  终身累计（``a_2d09364ee9ca``/``a_6226583b011a``/``a_83dd9e5b9dbc``）；
  (d) 纯飘链的兑现门槛是"爆头或者手里还留着财神"，不是只看爆头
  （``a_a05f75df4242`` r3）。第二轮跑全量后 (d) 那条门槛又反过来被证伪：
  ``a_737073f58d2a`` r5、``a_8e3e3e0b75aa`` r8 跟 ``a_a05f75df4242`` r3
  结构几乎一样（飘掉 1 张、手里还剩 1 张财神、没爆头），唯一差别是这两局
  的胡牌人手上有一副"吃"，日志都不给财飘——改成"手里还留着财神"这条路径
  只对纯暗手（meld_groups=0）有效，一旦吃/碰/杠过就必须真爆头才行。第三
  轮全量确认 0 不一致（见下面 SIM_COVERAGE，两条 False 已改为 True）。
- 3c（G2.1 联调后重做，用 ``mj.sim.snapshot.build_snapshot`` + 精确 seq
  对齐，替换掉第一版"手牌内容 + wall_remaining 启发式候选点"的做法——
  那版对不上号/选错候选点的比例太高，站不住）：``logs/*.jsonl`` 每条
  ``kind=="decision"`` 记录带的 ``state_hash`` 和 ``kind==
  "state_request_metric"`` 记录用**同一个**规范化函数算出来，可以借后者
  的 ``returned_seq`` 精确定位这条决策对应真实服务端事件号，把
  ``tools/models/events/<game_id>.json``（事件自带全局单调 ``seq``，
  跨整场比赛不重置）强制回放到那个位置，用 ``build_snapshot`` 生成快照
  逐字段比对（细节见 ``tools/sim_snapshot_check.py`` 模块 docstring）。
  全量 8 个日志文件、105.6 万条决策记录，99.73% 精确对齐（99.0 万条，
  对不上号 0.27%，达标验收线 <1%）：
  - ``wall_remaining``/``hand``/``melds``/``catch_play``/
    ``god_discarder_seat``/``dealer``/``round_no``/``scores``：
    **0 不一致**。``wall_remaining`` 恒等于服务端 +1（庄家起手隐藏摸牌
    服务端算、引擎不算，已经写进 ``mj.sim.snapshot`` 的换算里）。
  - ``chain_count`` 第一遍有 1242/99 万（0.125%）不一致，5 个逐例排查
    全部是同一种模式：非 chain_owner/god_discarder_seat 的座位自己做
    决策时，引擎按"全局"报真实链数，服务端按座位过滤、报 0——**服务端
    chain_count 是按座位下发的，不是全局广播，只有有资格兑现的座位自己
    能看到**。已经在 ``mj.sim.snapshot.build_snapshot`` 里补上这个过滤
    （``seat_owns_chain`` 判据），这是本轮唯一一条真正影响 G2 快照正确性
    的发现——线上 ``mj/hu_strategy.py`` 的飘决策如果被喂进"全局"
    chain_count 会高估自己根本兑现不了的链。修完后全量重跑，
    不一致降到 170/99 万（0.017%），降了 7 倍；剩下这一小撮没有继续
    逐例深挖，标注为更小的已知残留。
  - ``piao``：0.032% 残留，5 个逐例排查全部同一种模式：这个座位真的自由
    飘过白（``god_discarder_seat`` 就是它自己，chain_count 也对得上），
    引擎按规则正确记了 ``piao_count[seat]=1``，但真实日志的 ``piao``
    字段还是 0——跟既有结论一致（服务端这个字段本来就从不下发真实值，
    恒为 0，见本文件开头"chain_count/piao"一条 n=22446 的既有核实），
    不是新 bug，``piao_count`` 是我们自己按discard 事件推出来、专供
    ``mj.rules.evaluate()`` 算"4个白板"用的内部量，服务端原始快照压根
    不携带这个信息，没有可比对的真值，如实标注为"确认符合已知限制"，
    不需要改。
  - ``drawn_tile``（曾经 3.917%）/``responding_seats``（曾经
    17.005%）：**已定位并修复，不再是已知边界**。三个独立根因：
    (a) 庄家起手 14 张这张隐藏摸牌，服务端把它当 ``drawn_tile`` 报给庄家
    的第一次决策（样本 ``a_b478b2cbc5db_r1_b8_t0`` 第 1 局
    returned_seq=0），``_replay_to_seq`` 原来没摆这张，已经补上（不影响
    ``drawn_count``/``wall_remaining``）；(b) 碰/吃之后 ``drawn_tile``
    应该是空的，不是刚碰/吃到的那张——第一版工具用不精确对齐猜过一次
    "显示碰/吃到的那张"，被精确 seq 对齐证伪（``mj.sim.snapshot`` 的
    ``active_tile`` 已改回碰/吃后置空）；(c) ``returned_seq`` 本身经常
    比"这个状态真正成立的那个事件"偏大 1~2（同一个规范化 state_hash 被
    连续轮询命中好几次，``responding_seats`` 已经被后续别家的 pass
    改变但服务端快照没跟着刷新——``tools/sim_snapshot_check.py`` 改成
    "往前退最多 5 格、找第一个 phase+responding_seats 都对上的" 自纠
    （用日志自带的真值做校验，不是瞎猜），全量数据上退 1 格和退 2 格
    的都有真实样本。三条修完，两个字段在独立测试的两天日志样本
    （logs/2026-09-22.jsonl 前 200 组 + logs/2026-09-25.jsonl 前 300
    组）上都是 0 不一致；全量结果见交付时贴的命令输出。

已知的模拟能力边界（诚实声明，随代码演进维护，不要在报告里夸大）：
  SIM_COVERAGE = {
      "self_draw": True, "peng_gang_window_all_seats_queried": True,
      "chi_window_next_seat_only": True, "joker_claim_restriction": True,
      "max_two_chi": True, "gang_all_kinds": True,
      "gang_wall_tail_forbidden": True, "catch_play_circle": True,
      "chain_piao_global_reset_on_return": True,
      "dealer_rotation_8_rounds": True,
      "legal_action_self_play": "unvalidated",  # 见下方说明
      "gang_piao_combo_chain": True,   # 杠与飘共用同一条 chain_count 链，
                                        # 见上面 docstring 第 1 条，
                                        # a_101ea8afdf09/a_9cffd9377e9c 两个
                                        # 样本验证。
      "four_white_board_bonus": True,  # piao_count 按座位累计、喂给
                                        # mj.rules.evaluate() 现成的 piao
                                        # 形参，见上面 docstring 第 4 条，
                                        # a_3ede5bc381f1 样本验证。
  }
"强制回放"路径（tools/sim_replay_check.py：把日志里已经发生的动作喂给引擎，
只验证合法性与状态一致）已经用真实日志跑过验收（数字见交付报告）。
"引擎自己决定谁能吃碰杠、自己推进整局"这条自对弈路径（``legal_responses``/
``Match.play_round_with_actor``）只经过人工通读，没有独立的执行验证——
阶段二真正要用它做蒙特卡洛之前，必须先补一批"引擎自己打完整局、结算自洽"
的测试，不能只依赖强制回放路径间接证明它。
"""
import copy
import random

from ..melds import can_chi
from ..rules import baotou, evaluate, payout
from ..tiles import ALL_TILES, INDEX_TILE, JOKER, JOKER_IDX, TILE_INDEX, to_counts

TOTAL_TILES = 136
DEAL_SIZE = 13
# 流局阈值：实证见模块 docstring（n=6，全部一致）。用与既有工具同一口径的
# "wall_remaining = 136 - 起手发牌 - 已摸张数（含杠后补牌）"，停摸时它是 21。
WALL_EXHAUST_REMAINING = 21
GANG_FORBIDDEN_REMAINING = 21   # 服务端原始口径是 wall_remaining<=20 禁杠
                                 # （mj/responses.py 等既有代码用的就是这个
                                 # 服务端口径，不需要改）；引擎口径比服务端
                                 # 多 1（3c 用 37 万条真实决策快照核对出来的
                                 # 全局固定偏移，见模块 docstring），换算成
                                 # 引擎口径就是 <=21 禁杠。之前这里照抄了
                                 # 服务端的数字 20、没做换算，是一个真实的
                                 # 口径混用 bug——``tools/gang_threshold_check.py``
                                 # 对全量 3298 个文件 5769 次真实杠事件实测，
                                 # 发生杠时引擎 wall_remaining 最小值是 24，
                                 # 全部 >=22，和"<=21 禁杠"换算后的结论一致
                                 # （不是恰好卡在边界，样本里没出现 22/23 的
                                 # 真实杠，但没有任何一次低于这个换算阈值，
                                 # 支持而非证伪）。
CHAIN_CAP = 3   # 实证很薄（n=1 见到"三财飘"），如实标注，见模块 docstring。


class IllegalActionError(Exception):
    """强制回放/自对弈路径发现一个动作在当前状态机看来不合法。"""


class RoundEngine:
    """单局（一庄到胡/流局）状态机。可以：
    (a) 强制回放模式：外部按真实日志顺序调用 apply_*，每次先做合法性检查，
        检查失败抛 IllegalActionError；流局/自摸都由引擎自己的状态判定
        （``wall_remaining()``/``can_self_draw_hu()``），不接受外部"直接
        告诉我结果"的捷径——``tools/sim_replay_check.py`` 只在日志说
        "本局胡了"时调用 ``apply_hu(日志给的胜者座位)`` 去验证这个座位在
        引擎看来是否真的能自摸（因为日志里没有单独的"胡"事件，赢家是在
        round_ended 才第一次出现），流局完全不需要任何外部驱动，引擎自己
        摸到 wall_remaining<=21 就会 ``_settle_draw()``。
    (b) 自对弈模式（未执行验证，见模块 docstring）：外部用 legal_responses()
        查询当前谁能做什么，自己决定后调用同一批 apply_*。
    两种模式共用同一套状态与合法性判断。
    """

    def __init__(self, tiles, dealer, round_no, scores=None):
        self._init_common(dealer, round_no, scores)
        self._deck = list(tiles)
        for _ in range(DEAL_SIZE):
            for seat in range(4):
                self.hands[seat].append(self._deck.pop())

    @classmethod
    def from_known_hands(cls, hands, dealer, round_no, scores=None):
        """强制回放专用：起手牌直接来自日志，不需要洗牌/发牌，也不需要
        ``_deck``（回放全程用 ``forced_tile``，永远不会真的从牌堆里摸）。
        用一个专门的构造入口而不是在调用方手动摆一遍所有字段——之前
        tools/sim_replay_check.py 手动复制 __init__ 的初始字段，新增
        ``chain_owner`` 时漏改了一处，导致 AttributeError，这类同步 bug
        以后不会再犯（新增字段只需要改这一个地方）。"""
        engine = cls.__new__(cls)
        engine._init_common(dealer, round_no, scores)
        engine.hands = [list(h) for h in hands]
        engine._deck = []
        return engine

    def _init_common(self, dealer, round_no, scores):
        self.dealer = dealer
        self.round_no = round_no
        self.scores = list(scores) if scores is not None else [0, 0, 0, 0]
        self.hands = [[] for _ in range(4)]
        self.discards = [[] for _ in range(4)]
        self.last_discard = None   # 最近一次打出的牌（服务端快照的 last_discard：整局持续保留，直到下一次弃牌）
        self.melds = [[] for _ in range(4)]
        self.drawn_count = 0     # 全桌累计摸牌数（含杠后补牌），流局判据见 WALL_EXHAUST_REMAINING
        self.turn = dealer
        self.phase = "draw"      # draw | response | finished
        self.drawn_tile = None
        self.active_tile = None  # 供 mj.sim.snapshot 生成"服务端 drawn_tile"
                                  # 用：真摸牌时（``step_draw``）跟着
                                  # ``drawn_tile`` 一起置为摸到的那张；碰/吃
                                  # 之后是空的，不是刚碰/吃到的那张——第一版
                                  # 曾经猜成"碰/吃之后显示刚碰/吃到的那张"，
                                  # 被 G2.1 3c 精确 seq 对齐证伪：
                                  # a_b478b2cbc5db_r1_b8_t0 第 1 局
                                  # returned_seq=107，seat0 吃 2b 之后弃另一张
                                  # 2b，真实 drawn_tile=""，不是"2b"（那版猜测
                                  # 是用还没做精确对齐的第一代工具得出的，样本
                                  # 本身没对齐对）。不影响任何合法性判断，只是
                                  # 多记一个字段给 mj.sim.snapshot 用。
        self.chain_count = 0     # 全局"动作链"计数：飘（自由弃财神）和杠（暗/明/补，
                                  # 见 apply_claim/apply_gang）都算一次链上动作，统一计数，
                                  # 不是两套机制——real 语料证伪过"杠单独用 gang_open
                                  # 标志"的旧设计，见模块 docstring H1/H2。
        self.chain_owner = None  # 这条链的"番数归属人"——只在自由选择的飘/任意一次杠
                                  # 时更新为动作人，和 god_discarder_seat（响应豁免，
                                  # 逐次财神弃牌都会转移，含被迫）是两个不同的概念。
        self.chain_has_gang = False   # 这条链里是否出现过杠——决定 (a) 胡牌时是否
                                       # 需要爆头才能兑现链的番数倍数（见 H3：纯飘链需要
                                       # 爆头，含杠的链不需要），(b) 回放核对工具做
                                       # detail 标签映射时判断该用"财飘"族还是"连杠"/
                                       # "杠飘链"族文本。
        self.chain_has_piao = False   # 同上，配合 chain_has_gang 供 detail 映射区分
                                       # "连杠×N"（纯杠）还是"杠飘链×N"（杠+飘混合）。
        self.piao_count = [0, 0, 0, 0]   # 每座位"当前这条链"里自由弃出财神的
                                          # 次数——跟着 chain_count 一起清零/接管
                                          # （见 _advance_catch_play/_advance_gang_chain），
                                          # 不是终身累计。喂给 mj.rules.evaluate() 的
                                          # piao 参数，配合胡牌时手里还剩的财神数
                                          # 判定"4个白板"（见 H4）。
        self.catch_play = False
        self.god_discarder_seat = None
        self.response_stage = None    # None | "peng" | "chi"
        self.window_tile = None
        self.window_seat = None
        self.responding_seats = []
        self.finished = False
        self.is_draw = False
        self.winner = None
        self.fan = None
        self.detail = None
        self.could_self_draw_hu = None   # 每次摸牌后记一次"这一刻是否能自摸"，
                                          # 供 tools/sim_replay_check.py 统计
                                          # "能胡但没胡"的次数（正常现象，不算错）。

    # ------------------------------------------------------------------ 基础只读
    def wall_remaining(self):
        return TOTAL_TILES - DEAL_SIZE * 4 - self.drawn_count

    def meld_groups(self, seat):
        return len(self.melds[seat])

    def needs_draw(self):
        """当前 turn 座位是否该先摸一张牌。碰/吃之后 ``drawn_tile`` 也是 None，
        但那时座位手里已经是"摸牌后"的张数（13-3*副露组数+1），必须直接弃牌、
        不能再摸——驱动循环（``tools/arena2.py``/``mj/mc/rollout.py``）曾经只
        看 ``drawn_tile is None`` 就摸牌，导致碰/吃过的座位多摸一张牌（手牌
        11->12、永远凑不出 11 张的自摸型），整局胡牌率被系统性压低。"""
        return (self.phase == "draw" and self.drawn_tile is None
                and len(self.hands[self.turn]) == DEAL_SIZE - 3 * len(self.melds[self.turn]))

    def clone(self):
        return copy.deepcopy(self)

    def _next_seat(self, seat):
        return (seat + 1) % 4

    # ------------------------------------------------------------------ 摸牌
    def step_draw(self, forced_tile=None):
        """当前 turn 座位摸一张。``forced_tile``：回放模式下日志里记录的真实
        牌面；不给则从内部牌堆摸（自对弈模式，未执行验证）。流局判定统一走
        这里和 ``_advance_after_no_claim``：wall_remaining<=21（实证阈值）
        就地结束，强制回放和自对弈用同一套判据，不搞两条口径。"""
        if self.phase != "draw":
            raise IllegalActionError("摸牌时机不对：phase=%s" % self.phase)
        if self.wall_remaining() <= WALL_EXHAUST_REMAINING:
            self._settle_draw()
            return None
        tile = forced_tile if forced_tile is not None else self._deck.pop()
        self.hands[self.turn].append(tile)
        self.drawn_count += 1
        self.drawn_tile = tile
        self.active_tile = tile
        self.could_self_draw_hu = bool(self.can_self_draw_hu())
        return tile

    # ------------------------------------------------------------------ 判胡（不强制）
    def can_self_draw_hu(self, must_kaoxiang=False):
        """当前 turn 座位摸完牌后是否能自摸（不代表一定要胡，见 §背景）。

        链倍数（chain_count）能否算在当前摸牌座位头上，判据是"座位是不是
        chain_owner 或者当前 god_discarder_seat 之一"——不是单纯 chain_owner
        （被 a_61537f801625_r1_b0_t0 第 7 局证伪：seat3 自由飘之后，seat0
        被迫打出自己摸到的财神、豁免转移到 seat0，chain_owner 仍是 3，但
        seat0 自摸时日志给了"财飘"，说明当前豁免持有人也能兑现链——即使
        他自己没有主动飘过）；也不是单纯 god_discarder_seat（被
        a_25726d650449_r1_b6_t0 第 2 局证伪：seat0 先飘，seat3 之后被迫打出
        摸到的财神、豁免转移到 seat3，chain_owner 仍是 0，seat0 自摸时日志
        仍然给了"财飘"，说明原始飘出者只要没被"轮回到自己却不再飘"清零，
        也能兑现）。两个条件取"或"，两个真实案例都能同时满足。

        纯飘链（chain_has_gang=False）额外要求胡牌时刻是爆头（listens
        any-tile），或者手里还留着财神**且是纯暗手（没有吃/碰/杠、
        meld_groups=0）**，才兑现链倍数——三个真实案例互相证伪，逼出这个
        组合条件，不能只留一两条：
        - a_1b7d9ba3c608_r1_b5_t0 第 1 局：seat3 自由飘白之后紧接着自摸，
          chain_owner=seat3=胡牌人本人，没爆头、手里也没有财神了（起手只
          有 1 张，已经飘掉），日志给的是纯"平胡"（fan=1）——不爆头且手上
          没财神时整条链都不算。
        - a_a05f75df4242_r1_b6_t0 第 3 局：seat1 起手 2 张财神，飘掉 1 张之后
          紧接着自摸，纯暗手（没有任何吃碰杠），同样没有爆头，但胡牌时
          手里还剩 1 张财神——日志给了"财飘"（fan=2）。
        - a_737073f58d2a_r1_b3_t0 第 5 局、a_8e3e3e0b75aa_r1_b0_t0 第 8 局：
          跟上一条几乎同构（同样飘掉 1 张之后手里还剩 1 张财神、没爆头），
          唯一的差别是这两局胡牌人手上有一副"吃"（meld_groups=1，不是纯
          暗手），日志都是纯"平胡"（fan=1），不给财飘——说明"手里还留着
          财神"这条路径只对纯暗手有效，一旦吃/碰/杠过，光凭手里还有财神
          不够，必须真正爆头才行。
        含杠的链（chain_has_gang=True）没有这几条要求——
        a_101ea8afdf09_r1_b0_t0 第 3 局：seat3 连续暗杠+补杠、最后一次杠后
        补牌直接自摸财神，detail=['平胡','连杠×2']（fan=4），全程没有爆头
        标签、手里也没剩财神，链倍数照样兑现。"""
        if self.drawn_tile is None:
            return None
        seat = self.turn
        pre = list(self.hands[seat])
        pre.remove(self.drawn_tile)
        counts13 = to_counts(pre)
        draw_idx = TILE_INDEX[self.drawn_tile]
        meld_groups = self.meld_groups(seat)
        effective_chain = 0
        if self.chain_count and (seat == self.chain_owner or seat == self.god_discarder_seat):
            holds_joker_concealed = counts13[JOKER_IDX] and meld_groups == 0
            if self.chain_has_gang or holds_joker_concealed or baotou(counts13, meld_groups):
                effective_chain = self.chain_count
        return evaluate(counts13, draw_idx, effective_chain, self.piao_count[seat],
                        meld_groups, must_kaoxiang=must_kaoxiang, gang_open=False)

    # ------------------------------------------------------------------ 自摸
    def apply_hu(self, seat):
        if self.phase != "draw" or seat != self.turn or self.drawn_tile is None:
            raise IllegalActionError("自摸时机不对：phase=%s turn=%s seat=%s" % (self.phase, self.turn, seat))
        result = self.can_self_draw_hu()
        if not result:
            raise IllegalActionError("座位 %d 当前牌不构成自摸" % seat)
        self.finished = True
        self.winner = seat
        self.fan = result["fan"]
        self.detail = result["detail"]
        self._apply_settlement(seat, result["fan"])
        return result

    def _apply_settlement(self, winner, fan):
        is_dealer = winner == self.dealer
        if is_dealer:
            gain = payout(fan, dealer=True)
            self.scores[winner] += gain
            for s in range(4):
                if s != winner:
                    self.scores[s] -= 8 * fan
        else:
            gain = payout(fan, dealer=False)
            self.scores[winner] += gain
            self.scores[self.dealer] -= 8 * fan
            for s in range(4):
                if s != winner and s != self.dealer:
                    self.scores[s] -= 1 * fan

    def _settle_draw(self):
        self.finished = True
        self.is_draw = True
        self.winner = None
        self.phase = "finished"

    # ------------------------------------------------------------------ 弃牌
    def apply_discard(self, seat, tile):
        """飘（弃财神续动作链）不是单独一种服务端事件——日志里就是一次
        普通的 ``tile_discarded``。这里按规则本身判定：自由选择（不是抓打圈
        逼着你只能打刚摸到的那张）打出财神，就是飘，链数 +1；不要求打完
        之后手牌"依然爆头"——最初按"爆头"做门槛，被
        a_a05f75df4242_r1_b6_t0 第 3 局证伪：seat1 自由打出财神时手牌根本
        没到爆头（listens any-tile）的程度，但真实 detail 依然是"财飘"、
        fan 翻倍，说明"爆头"不是必要条件，只要是自由选择打财神就算。"""
        if self.phase != "draw" or seat != self.turn:
            raise IllegalActionError("弃牌时机不对：phase=%s turn=%s seat=%s" % (self.phase, self.turn, seat))
        if tile not in self.hands[seat]:
            raise IllegalActionError("座位 %d 手里没有 %s" % (seat, tile))
        if self.catch_play and seat != self.god_discarder_seat and tile != self.drawn_tile:
            raise IllegalActionError("抓打圈内座位 %d 只能打刚摸到的 %s，不能打 %s" % (
                seat, self.drawn_tile, tile))
        is_joker_discard = tile == JOKER
        # 三件不同的事，不要合成一个标志（三个真实样本各自逼出一条）：
        #   (1) catch_play 布尔值 + god_discarder_seat（响应豁免/弃牌限制的
        #       归属）——任何财神弃牌都会把 god_discarder_seat 转移到当前
        #       弃牌人，不看是不是被迫的（实证 n=69296；a_15390db3aaea_r1_
        #       b1_t0 第 2 局进一步证实：seat1 被迫打出摸到的财神之后，
        #       后续碰窗口的豁免确实转移到了 seat1，不是继续留在原触发者）；
        #   (2) chain_owner（番数归属）——只在"自由选择"打出财神时才更新：
        #       不在抓打圈里，或者恰好是当前豁免者自己的回合。被迫打出的
        #       财神不改变 chain_owner——a_25726d650449_r1_b6_t0 第 2 局：
        #       seat0 先自由飘出financial（chain_owner=0），seat3 之后被迫
        #       打出自己摸到的财神（god_discarder_seat 转移到 3，但
        #       chain_owner 仍是 0），seat0 自摸时 fan=4，链倍数算在 seat0
        #       头上，不是 seat3；
        #   (3) 是否真的记一次飘（chain_count+=1）——就等于 (2) 是否成立，
        #       不需要额外的"打完之后依然爆头"条件（见下方 apply_discard
        #       docstring 的证伪案例）。
        free_choice = is_joker_discard and ((not self.catch_play) or (seat == self.god_discarder_seat))
        is_piao = free_choice
        self.hands[seat].remove(tile)
        self.discards[seat].append(tile)
        self.last_discard = tile
        if is_piao:
            if self.chain_count >= CHAIN_CAP:
                raise IllegalActionError("动作链已封顶（%d），不能再飘" % CHAIN_CAP)
            self.chain_count += 1
            self.chain_owner = seat
            self.chain_has_piao = True
            self.piao_count[seat] += 1   # 只反映当前这条链，跟着清零/接管，见 H4/_init_common。
        self.drawn_tile = None
        self.active_tile = None
        self._advance_catch_play(seat, is_joker_discard=is_joker_discard, is_piao=is_piao)
        self.window_tile = tile
        self.window_seat = seat
        if is_joker_discard:
            # 财神不进任何响应窗口（不可被吃/碰/杠）。
            self._advance_after_no_claim(seat)
            return
        peng_seats = self._peng_window_seats(seat)
        if peng_seats:
            self.phase = "response"
            self.response_stage = "peng"
            self.responding_seats = peng_seats
        else:
            self._open_chi_window_or_advance(seat, tile)

    def _advance_catch_play(self, discarder_seat, is_joker_discard, is_piao):
        """抓打圈布尔值 + 响应豁免归属（``god_discarder_seat``）：任何财神
        弃牌都置 catch_play=True 且把豁免转移到当前弃牌人，不看是不是被迫
        的——实证 n=69296（catch_play 布尔值）+ a_15390db3aaea_r1_b1_t0 第
        2 局（豁免确实跟着转移：seat1 被迫打财神之后，后续碰窗口的豁免者
        变成了 seat1）。这和"链的番数归属"（``chain_owner``，只在自由飘时
        更新）是两回事，不要合并，见 ``apply_discard``。

        清零条件是"轮回到 chain_owner 自己、且这次不是再飘"——不是"轮回到
        当前豁免者 god_discarder_seat"！这两个曾经被错误合并过一次，被
        a_12bd85124f44_r1_b4_t0 第 8 局证伪：seat0 自由飘（chain_owner=0），
        seat2 之后被迫打出摸到的财神（god_discarder_seat 转移到 2，
        chain_owner 仍是 0），seat3 弃牌不是财神，接着轮到 seat2（当前豁免
        者）自己弃牌、不是再飘——这时如果按"豁免者返回即清零"，chain_count
        会被错误清成 0，但真实 fan 是按 chain_count=1 结算的（seat0 后来
        自摸，链没有被清）。两个清零各自独立判定，互不影响：
        - god_discarder_seat 的豁免只在"当前豁免者自己"返回且不再飘时解除
          （这条决定响应窗口豁免/弃牌限制，控制不到链）；
        - chain_count/chain_owner 只在"chain_owner 自己"返回且不再飘时清零
          （这条决定番数，可能在 catch_play 早就解除之后才轮到）。

        第二轮返修再证伪一次：chain_owner 自己返回时，如果当前 god_discarder_
        seat 是另一个仍然活跃（还没轮到自己返回）的座位，不能把 chain_count
        整个清零——那样会连带废掉"当前豁免持有人也能兑现链"这条资格（见
        ``can_self_draw_hu`` 的判据）。被 ``a_61537f801625_r1_b0_t0`` 第 7 局
        证伪：seat3 自由飘（chain_owner=3），seat0 之后被迫打出自己摸到的
        财神（god_discarder_seat 转移到 0，chain_owner 仍是 3），seat3 后来
        轮到自己、正常弃牌（discarder==chain_owner=3，不是再飘）——这里如果
        直接清零 chain_count，seat0 紧接着自摸时就再也兑现不了这条链，但
        真实 fan=4（财飘+爆头）。正确做法是只清 chain_owner 自己这一条资格
        （连带清掉他本局这条链的 piao_count，因为已经"退出"了），chain_count
        本身留给 god_discarder_seat 那条资格继续用；只有当 god_discarder_
        seat 也已经解除（是 None）或者恰好就是同一个人时，才真的把
        chain_count 清零（両条资格都交回，链彻底作废）。反过来，
        god_discarder_seat 自己解除时如果 chain_owner 已经是 None（另一条
        资格也已经交回），这里顺手把 chain_count 也清掉，防止残留的旧计数
        被后面一次完全不相关的新飘/杠误当成"接着算"。"""
        if is_joker_discard:
            self.catch_play = True
            self.god_discarder_seat = discarder_seat
        elif self.catch_play and discarder_seat == self.god_discarder_seat:
            self.catch_play = False
            self.god_discarder_seat = None
            if self.chain_owner is None:
                self.chain_count = 0
                self.chain_has_gang = False
                self.chain_has_piao = False
        if not is_piao and discarder_seat == self.chain_owner:
            self.piao_count[discarder_seat] = 0
            self.chain_owner = None
            if self.god_discarder_seat is None or self.god_discarder_seat == discarder_seat:
                self.chain_count = 0
                self.chain_has_gang = False
                self.chain_has_piao = False

    def _peng_window_seats(self, discarder_seat):
        """碰（含明杠）窗口：服务端总是问全部非打牌方，不看手牌是否真的
        够——实证见模块 docstring（94% 的无声明弃牌，响应集合正好是另外
        3 家；不足 3 家的都对应"有人直接声明、其余人的响应未被记录"）。
        抓打圈期间只问触发者本人。"""
        if self.catch_play:
            return [self.god_discarder_seat] if self.god_discarder_seat != discarder_seat else []
        return [s for s in range(4) if s != discarder_seat]

    def _chi_window_seat(self, discarder_seat):
        """吃窗口只问下家；抓打圈期间同样只有触发者本人能吃。返回 None
        表示这一轮没有吃窗口（下家被抓打圈限制，或轮空——4 人桌下家恒存在，
        这里只处理抓打圈限制这一种"没有吃窗口"的情形）。"""
        chi_seat = self._next_seat(discarder_seat)
        if self.catch_play and self.god_discarder_seat != chi_seat:
            return None
        return chi_seat

    def _open_chi_window_or_advance(self, discarder_seat, tile):
        """吃窗口对下家无条件打开（抓打圈例外只问触发者本人），不看这张牌
        是不是数牌、下家吃摊够不够——服务端照样会问，下家没资格就只能 pass
        （实证：日志里字牌弃牌也观察到吃窗口的 pass/timeout，见 mj.sim.engine
        模块 docstring）。真正的"能不能吃"判断留给 ``apply_claim`` 处理，
        这里只决定"要不要开这个窗口"。"""
        chi_seat = self._chi_window_seat(discarder_seat)
        if chi_seat is not None:
            self.phase = "response"
            self.response_stage = "chi"
            self.responding_seats = [chi_seat]
        else:
            self._advance_after_no_claim(discarder_seat)

    def _chi_options(self, seat, tile):
        idx = TILE_INDEX.get(tile)
        if idx is None or idx >= 27 or tile == JOKER:
            return []
        out = []
        for offsets in ((-2, -1), (-1, 1), (1, 2)):
            idxs = [idx + o for o in offsets]
            if min(idxs) < 0 or max(idxs) >= 27 or any(i // 9 != idx // 9 for i in idxs):
                continue
            names = [INDEX_TILE[i] for i in idxs]
            if all(self.hands[seat].count(n) for n in names):
                out.append(names)
        return out

    def _advance_after_no_claim(self, discarder_seat):
        self.window_tile = None
        self.window_seat = None
        self.response_stage = None
        self.responding_seats = []
        self.turn = self._next_seat(discarder_seat)
        # 流局在这里自己判定，不能只指望 step_draw：真实流局是"最后一次弃牌
        # 的碰窗口+吃窗口都无人声明之后直接结束"，根本没有再尝试摸下一张
        # （见模块 docstring 的实证），所以这个"没人声明、准备轮到下一家"
        # 的时刻，必须自己检查一次墙尾，不能靠一次永远不会发生的 step_draw
        # 调用来发现流局。
        if self.wall_remaining() <= WALL_EXHAUST_REMAINING:
            self._settle_draw()
            return
        self.phase = "draw"

    # ------------------------------------------------------------------ 响应（碰/吃/杠/过）
    def apply_pass(self, seat):
        if self.phase != "response" or seat not in self.responding_seats:
            raise IllegalActionError("座位 %d 现在没有响应窗口可以 pass" % seat)
        self.responding_seats.remove(seat)
        if self.responding_seats:
            return
        if self.response_stage == "peng":
            self._open_chi_window_or_advance(self.window_seat, self.window_tile)
        else:
            self._advance_after_no_claim(self.window_seat)

    def apply_claim(self, seat, kind, tiles, auto_draw=True):
        """kind: "peng" | "chi" | "gang"（明杠，来自弃牌，和碰共用一个窗口——
        见 mj/responses.py::response_gang_take 的既有说明）。强制回放模式
        直接信日志的声明种类，只检查这个座位这个动作当下合不合法。"""
        expected_stage = "chi" if kind == "chi" else "peng"
        if self.phase != "response" or seat not in self.responding_seats or self.response_stage != expected_stage:
            raise IllegalActionError("座位 %d 现在没有 %s 的响应窗口（当前阶段 %s）" % (
                seat, kind, self.response_stage))
        tile = self.window_tile
        if tile == JOKER:
            raise IllegalActionError("财神不能被吃/碰/杠")
        if kind == "peng":
            if self.hands[seat].count(tile) < 2:
                raise IllegalActionError("座位 %d 碰 %s 张数不够" % (seat, tile))
            self.hands[seat].remove(tile)
            self.hands[seat].remove(tile)
            self.melds[seat].append({"kind": "peng", "tiles": [tile, tile, tile]})
        elif kind == "chi":
            if seat != self._next_seat(self.window_seat) or not can_chi(self.melds[seat]):
                raise IllegalActionError("座位 %d 不能吃这张 %s" % (seat, tile))
            if list(tiles) not in self._chi_options(seat, tile):
                raise IllegalActionError("座位 %d 吃的组合 %s 不合法" % (seat, tiles))
            for t in tiles:
                self.hands[seat].remove(t)
            self.melds[seat].append({"kind": "chi", "tiles": list(tiles) + [tile]})
        elif kind == "gang":
            if self.hands[seat].count(tile) < 3 or self.wall_remaining() <= GANG_FORBIDDEN_REMAINING:
                raise IllegalActionError("座位 %d 明杠 %s 不合法" % (seat, tile))
            for _ in range(3):
                self.hands[seat].remove(tile)
            self.melds[seat].append({"kind": "gang", "sub": "ming", "tiles": [tile] * 4})
        else:
            raise IllegalActionError("未知声明类型 %s" % kind)
        self.window_tile = None
        self.window_seat = None
        self.response_stage = None
        self.responding_seats = []
        self.turn = seat
        self.phase = "draw"
        self.drawn_tile = None
        self.active_tile = None   # 碰/吃之后 drawn_tile 是空的，不是刚碰/吃
                                   # 到的那张——G2.1 3c 精确 seq 对齐用真实
                                   # 决策快照证伪过一次尝试把它设成 tile 的
                                   # 版本（那版是用第一代启发式候选点对齐时
                                   # 猜的，样本本身就没对齐对，见
                                   # tools/sim_snapshot_check.py 的诊断
                                   # 脚本：a_b478b2cbc5db_r1_b8_t0 第 1 局
                                   # returned_seq=107，seat0 吃 2b 之后弃
                                   # 另一张 2b，真实 drawn_tile=""，不是
                                   # "2b"）。
        # 碰/吃之后不摸牌，直接轮到该座位出牌：phase 保持 "draw"，但
        # drawn_tile=None——抓打圈判定（只能打刚摸到的）在碰/吃后不适用
        # （碰吃本身已经不在抓打圈范围内——财神打出才会进入抓打圈，
        # 财神不可被吃碰）。
        if kind == "gang":
            self._advance_gang_chain(seat)
            if auto_draw:
                self.step_draw()

    def apply_gang(self, seat, tile, kind, auto_draw=True):
        """kind: "an"（暗杠，摸牌方自己） | "bu"（补杠，把已有的碰升级）。
        （明杠走 apply_claim(kind="gang")，因为它来自响应窗口而不是自己的
        摸牌回合。）``auto_draw=False``：强制回放模式用——补杠动作本身先
        落地，紧跟着的补牌由日志里下一条 ``tile_drawn``（带
        ``gang_replenish`` 标记）事件驱动 ``step_draw(forced_tile=...)``。"""
        if tile == JOKER:
            raise IllegalActionError("财神不能杠")
        if self.wall_remaining() <= GANG_FORBIDDEN_REMAINING:
            raise IllegalActionError("墙尾 %d 张，禁杠" % self.wall_remaining())
        if kind == "an":
            if self.phase != "draw" or seat != self.turn or self.hands[seat].count(tile) < 4:
                raise IllegalActionError("座位 %d 暗杠 %s 不合法" % (seat, tile))
            for _ in range(4):
                self.hands[seat].remove(tile)
            self.melds[seat].append({"kind": "gang", "sub": "an", "tiles": [tile] * 4})
        elif kind == "bu":
            existing = next((m for m in self.melds[seat]
                            if m.get("kind") == "peng" and m["tiles"][0] == tile), None)
            if self.phase != "draw" or seat != self.turn or existing is None or tile not in self.hands[seat]:
                raise IllegalActionError("座位 %d 补杠 %s 不合法" % (seat, tile))
            self.hands[seat].remove(tile)
            self.melds[seat].remove(existing)
            self.melds[seat].append({"kind": "gang", "sub": "bu", "tiles": [tile] * 4})
        else:
            raise IllegalActionError("未知杠类型 %s" % kind)
        self.drawn_tile = None
        self._advance_gang_chain(seat)
        if auto_draw:
            self.step_draw()

    def _advance_gang_chain(self, seat):
        """杠（暗/明/补，三种都走到这里）算一次链上动作，和飘共用
        chain_count/chain_owner——real 语料证伪过"杠单独用 gang_open 标志、
        和飘的 chain_count 是两套互不相关机制"的旧假设：
        a_101ea8afdf09_r1_b0_t0 第 3 局，seat3 连续暗杠+补杠（中间没有任何
        弃牌打断），最后一次杠后补牌直接自摸财神，detail=['平胡','连杠×2']、
        fan=4——如果杠只贡献一次性的"杠开"×2，同一座位连续两次杠应该是
        "杠开"+"杠开"或者根本没有第二次的命名，但真实标签是统一的
        "连杠×2"（2 次链上动作、2**2=4），跟纯飘链"双财飘"的数值结构一模
        一样，只是文本按"链里全是杠/全是飘/杠飘混合"三选一命名（回放核对
        工具的 detail 映射表按这个规则重建文本，只比较 fan 数值本身不受
        影响）。杠永远算"自由选择"（catch_play 只限制弃牌，不限制杠），
        所以无条件更新 chain_owner，不像飘要看 free_choice。

        但杠的动作人如果跟当前链的归属人不是同一个座位，不能无条件累加到
        对方的链上——会接管、不是叠加。被 ``a_972a8d0f49ed_r1_b7_t0`` 第 6
        局证伪：seat2 自由飘白（chain_owner=2, chain_count=1），seat3（不是
        chain_owner，也不是当前抓打圈豁免的持有人，抓打圈只限制弃牌、不
        限制暗杠，见上）用自己刚摸到的牌暗杠、紧接着自摸，真实
        detail=['平胡','杠开']、fan=2——如果按无条件累加会变成
        chain_count=2、"杠飘链×2"、fan=4，多算了 seat2 那次跟这次杠完全
        无关的飘。杠一来，链就整个转手给这个杠的人重新算，不管前面攒了
        多少。"""
        if self.chain_owner is not None and seat != self.chain_owner:
            self.piao_count[self.chain_owner] = 0
            self.chain_count = 0
            self.chain_has_gang = False
            self.chain_has_piao = False
        if self.chain_count >= CHAIN_CAP:
            return   # 没有反证支持杠链也封顶在 3，但也没有更高链长的样本；
                      # 保守起见不对杠强行抛 IllegalActionError（弃牌那边的
                      # 封顶检查是自对弈用的防御性上限，回放模式不该因为一个
                      # 没证据支持的假设去拒绝真实发生过的杠）。
        self.chain_count += 1
        self.chain_owner = seat
        self.chain_has_gang = True

    # ------------------------------------------------------------------ 飘（爆头态弃财神续链）
    def apply_piao(self, seat, tile=JOKER):
        """薄封装：飘在规则上就是"打财神、打完仍爆头"的那次弃牌，判定与
        链数递增已经收口在 ``apply_discard`` 里。"""
        return self.apply_discard(seat, tile)

    # ------------------------------------------------------------------ 自对弈用：查询当前谁能做什么
    # 未执行验证（见模块 docstring）；强制回放路径不依赖这些函数。
    def legal_responses(self, seat):
        if self.phase != "response" or seat not in self.responding_seats:
            return []
        tile = self.window_tile
        out = ["pass"]
        if self.response_stage == "peng":
            if self.hands[seat].count(tile) >= 2:
                out.append("peng")
            if self.hands[seat].count(tile) >= 3 and self.wall_remaining() > GANG_FORBIDDEN_REMAINING:
                out.append("gang")
        elif self.response_stage == "chi":
            if can_chi(self.melds[seat]) and self._chi_options(seat, tile):
                out.append("chi")
        return out


def build_wall(seed):
    """确定性洗牌：136 张牌（34 种 x 4）。故意不 import mj.arena——那是旧策略
    评测模块，会把 mj.ev/mj.strategy 等生产打分逻辑一起带进 mj.sim 这条
    未来要跑蒙特卡洛的路径，不该有这条依赖边。逻辑和 mj.arena.wall 一样。"""
    rng = random.Random(seed)
    tiles = [tile for tile in ALL_TILES for _ in range(4)]
    rng.shuffle(tiles)
    return tiles


class Match:
    """一场 8 局：胡者接庄，流局庄家连庄（未执行验证——见模块 docstring，
    G1 的验收目前只覆盖单局强制回放）。"""

    def __init__(self, seed, rounds=8):
        self.seed = seed
        self.rounds = rounds
        self.scores = [0, 0, 0, 0]
        self.dealer = 0
        self.round_no = 1
        self.history = []

    def play_round_with_actor(self, actor):
        """actor(engine) -> None，在 engine 上反复调用 apply_* 直到 finished。
        调用方（tools/arena2.py）负责驱动 legal_responses/摸牌/出牌决策；
        本类只负责局与局之间的银行记账（换庄、局号推进）。"""
        wall_tiles = build_wall(self.seed * 1000 + self.round_no)
        engine = RoundEngine(wall_tiles, self.dealer, self.round_no, self.scores)
        actor(engine)
        if not engine.finished:
            raise IllegalActionError("actor 没有把整局打完（finished=False）")
        self.scores = engine.scores
        self.history.append({"round_no": self.round_no, "dealer": self.dealer,
                             "winner": engine.winner, "is_draw": engine.is_draw,
                             "fan": engine.fan, "detail": engine.detail})
        if not engine.is_draw and engine.winner is not None:
            self.dealer = engine.winner
        self.round_no += 1
        return engine
