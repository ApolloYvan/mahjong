"""正式赛事多阶段生命周期状态机。

官网规则核验已确认（非本次臆测）的赛事状态机：

    registering → running → stage_done → stage_open → running → …(决赛) → finished
    任意阶段可转 closed / void。

关键约束（均已由官网核验或独立技术评审报告确认，逐条对应修复）：
- **直接使用服务端 ``tournament_id``**（来自 ``/api/me``），不得从 ``game_id``
  字符串猜测赛事 ID（旧 `bot.wait_for_assignment` 用 `gid.rsplit("_", 2)[0]`
  之类的字符串切割，属于脆弱猜测，官网核验也明确指出应直接读
  ``/api/me`` 的 ``tournament_id``）。
- **ready 必须幂等**：多次调用不产生副作用；但每次真正进入 ``stage_open``
  （含同阶段号故障重赛后重新回到 ``stage_open``）都必须重新确认出席，
  不能靠「这个 stage.no 已经 ready 过」的缓存永久跳过——故障重赛会清空
  该阶段旧成绩，确认状态也需要重做。
- **``active_games`` 为空不等于赛事结束**：阶段间隙（如 ``stage_done``
  等待管理员推进到下一阶段）``active_games`` 会是空列表，此时应继续
  等待轮询，不能提前退出。唯一的进度真相是 ``tournament.status``。
- 晋级判定用 ``qualified``/``qualify_role`` 语义，不从全局 ``rank`` 猜测
  组内晋级（本模块负责状态机与生命周期，不负责晋级排名计算，只透传
  服务端返回的字段，交由上层赛事价值层——阶段5——使用）。

设计上把「决定下一步做什么」（``TournamentLifecycle.step``，纯函数、无 I/O，
可离线用录制的假响应序列 + 虚拟时钟测试）与「真正执行 I/O」
（``run_tournament``，负责调用 ``MahjongApi`` 并处理网络异常/429/断线重试）
分离，前者可完全离线测试覆盖全部状态转移，后者只负责把决定翻译成真实请求。
"""
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .api import ApiError

TERMINAL_STATUSES = {"finished", "closed", "void"}
# 已知非终态状态；未识别的状态一律当作非终态处理（继续等待轮询），
# 避免因为服务端引入新状态字面量就被误判为"已结束"而提前退出。
KNOWN_NON_TERMINAL = {"registering", "running", "stage_done", "stage_open"}


@dataclass
class TournamentSnapshot:
    """一次轮询得到的赛事状态快照（来自 /api/me + /api/tournaments/{id}）。"""

    tournament_id: Optional[str]
    status: Optional[str]
    active_games: List[Any] = field(default_factory=list)
    qualified: Optional[bool] = None
    qualify_role: Optional[str] = None
    raw_me: Dict[str, Any] = field(default_factory=dict)
    raw_tournament: Dict[str, Any] = field(default_factory=dict)


def _extract_game_id(item):
    """从单个 active_games/my_games 元素里取出 game_id：元素可以是字符串
    本身，也可以是携带 ``game_id`` 字段的对象。非法/缺失时返回 None。"""
    if item is None:
        return None
    if isinstance(item, str):
        stripped = item.strip()
        return stripped or None
    if isinstance(item, dict):
        # 明确属于观战/其他用户的条目一律忽略：接入指南要求 active_games
        # 只能反映"自己"的对局，不得把明确标记为观战或非本人座位的条目
        # 混入。只在字段明确给出信号时才排除（不做无根据的猜测）。
        if item.get("spectator") or item.get("is_spectator") or item.get("watching"):
            return None
        if "mine" in item and not item.get("mine"):
            return None
        if "is_mine" in item and not item.get("is_mine"):
            return None
        gid = item.get("game_id") or item.get("id")
        if gid is None:
            return None
        gid = str(gid).strip()
        return gid or None
    return None


def extract_active_game_ids(me, tournament_info):
    """统一发现"自己正在打的对局" game_id 列表——纯函数，不做任何 I/O。

    来源优先级（按接入指南）：
    1. ``tournament_info.my_games``（权威来源，赛事视角下的"我的对局"）；
    2. ``tournament_info.active_games``（兼容字段，同一份赛事快照里的
       备用命名）；
    3. ``me.active_games``（旧版全局身份视角的兼容字段）。

    只要命中优先级更高的来源且非空，就不再降级到下一个来源——不合并多个
    来源（避免把"赛事视角"和"全局视角"两份可能不一致的列表拼在一起，
    产生虚假的对局数）。

    每个来源内部：
    - 元素可以是字符串 game_id，也可以是携带 ``game_id``/``id`` 字段的
      对象；
    - 忽略空值、明确标记为观战（spectator/is_spectator/watching）或明确
      标记为非本人（mine=False/is_mine=False）的条目、以及解析不出
      game_id 的异常项——不崩溃，静默跳过；
    - 去重并保持首次出现的稳定顺序（同一 game_id 在同一来源或跨调用
      重复出现时只保留第一次的位置）。
    """
    me = me or {}
    tournament_info = tournament_info or {}

    for source in (
        tournament_info.get("my_games"),
        tournament_info.get("active_games"),
        me.get("active_games"),
    ):
        if not source:
            continue
        seen = set()
        ordered = []
        for item in source:
            gid = _extract_game_id(item)
            if gid is None or gid in seen:
                continue
            seen.add(gid)
            ordered.append(gid)
        if ordered:
            return ordered
    return []


@dataclass
class Decision:
    """状态机给出的下一步动作建议。"""

    action: str  # "register" | "ready" | "play" | "wait" | "done"
    reason: str = ""
    game_ids: List[str] = field(default_factory=list)


class TournamentLifecycle:
    """纯状态机：不做任何网络 I/O，只根据快照序列决定下一步动作。

    可用于离线单测：把录制或构造的 ``TournamentSnapshot`` 序列依次喂给
    ``step``，断言返回的 ``Decision`` 序列符合预期，完全不需要真实网络。

    **两阶段提交语义（修复 P0：register/ready 失败后不会重试）**：
    ``step()`` 只负责"根据当前已确认状态，判断接下来应该发出什么命令"，
    绝不在返回 ``register``/``ready`` 决定的同一刻就把内部状态标记为
    "已完成"。真正的状态提交必须由调用方（``run_tournament``）在对应的
    API 调用**确认成功**之后，显式调用 ``confirm_registered()`` /
    ``confirm_readied()``。如果 API 调用失败，调用方不调用 confirm_*，
    那么下一次 ``step()`` 在相同的快照状态下会**再次**返回同样的命令，
    从而形成"持续重试直到成功"的语义，而不是"发出过一次就永久跳过"。

    历史 bug（已被用户复核实测证实）：旧实现在 ``step()`` 内部、返回
    ``Decision(action="register"/"ready", ...)`` 之前就设置
    ``self._registered = True`` / ``self._readied_this_epoch = True``，
    导致 ``run_tournament()`` 捕获到 API 异常后即使什么都不做（`pass`），
    下一轮 ``step()`` 也会因为这些标志已经为真而不再重新发出命令——
    实测复现：``ready()`` 第一次返回 500 后，只要服务端状态一直保持
    ``stage_open``，``api.ready.call_count`` 永远停在 1，不会重试。
    """

    def __init__(self):
        self._last_status: Optional[str] = None
        self._registered = False
        self._readied_this_epoch = False
        self._done = False
        self._done_reason = ""

    @property
    def done(self) -> bool:
        return self._done

    def confirm_registered(self) -> None:
        """由调用方在 ``api.register()`` 调用确认成功后调用，提交
        "已报名"状态。在此之前，``step()`` 会持续返回 ``register`` 决定。"""
        self._registered = True

    def confirm_readied(self) -> None:
        """由调用方在 ``api.ready()`` 调用确认成功后调用，提交"本次
        stage_open/registering 周期已确认出席"状态。在此之前，``step()``
        会持续返回 ``ready`` 决定（每次 poll 都会重发，直到成功或状态
        本身推进离开当前周期）。"""
        self._readied_this_epoch = True

    def _entered_stage_open(self, snapshot: TournamentSnapshot) -> bool:
        """检测「刚进入 stage_open」这个边沿事件（供外部诊断/测试查询），
        真正的重置逻辑已内联到 ``step()`` 内部。"""
        return snapshot.status == "stage_open" and self._last_status != "stage_open"

    def step(self, snapshot: TournamentSnapshot) -> Decision:
        if self._done:
            return Decision(action="done", reason=self._done_reason)

        status = snapshot.status

        if status in TERMINAL_STATUSES:
            self._done = True
            self._done_reason = status
            return Decision(action="done", reason=status)

        if snapshot.tournament_id is None:
            # 还未拿到服务端 tournament_id，不能猜测，只能继续等待轮询。
            self._last_status = status
            return Decision(action="wait", reason="no_tournament_id")

        # 检测「刚进入 stage_open」的边沿：包括首次进入，以及同阶段号故障
        # 重赛后从别的状态（running/stage_done）重新回到 stage_open——这标志着
        # 一次新的确认周期开始，必须重置 _readied_this_epoch 以强制重新 ready。
        if status == "stage_open" and self._last_status != "stage_open":
            self._readied_this_epoch = False

        if not self._registered and status is None:
            # 报名前 /api/tournaments/{id} 可能还查不到状态；先报名。
            # 不在此处设置 self._registered——必须等待 confirm_registered()
            # 被显式调用（即 API 调用确认成功后）才会停止重复发出该决定，
            # 否则一次瞬时失败就会被状态机永久遗漏（P0 修复点）。
            self._last_status = status
            return Decision(action="register", reason="initial_register")

        # ready 需要确认的两种周期：initial registering、以及每次 stage_open。
        # 只要处于这两种状态且尚未 confirm_readied()，就持续重试 ready，
        # 不会因为"已经发过一次决定"就误判为已完成（P0 修复点）。
        if status in ("registering", "stage_open") and not self._readied_this_epoch:
            self._last_status = status
            return Decision(action="ready", reason=f"enter_{status}")

        if status == "stage_open":
            # 本次 stage_open 已确认出席（confirm_readied 已被调用），
            # 仍处于该状态（等待开赛），继续等待。
            self._last_status = status
            return Decision(action="wait", reason="stage_open_waiting")

        if status == "registering":
            # 已确认出席，registering 状态仍未推进，继续等待。
            self._last_status = status
            return Decision(action="wait", reason="registering_waiting")

        if status != self._last_status:
            # 状态发生变化（如 stage_open→running、running→stage_done），
            # 意味着离开了需要 ready 的周期，下次重新进入 stage_open 需要
            # 再次确认（由上面的边沿检测触发重置）。
            self._readied_this_epoch = False

        self._last_status = status

        if status == "running":
            if snapshot.active_games:
                game_ids = [
                    g.get("game_id") if isinstance(g, dict) else str(g)
                    for g in snapshot.active_games
                ]
                return Decision(action="play", reason="active_games", game_ids=game_ids)
            # running 但 active_games 为空：阶段间隙或刚推进，继续等待，
            # 绝不能当作赛事结束（已确认修复项：active_games 为空 ≠ 结束）。
            return Decision(action="wait", reason="running_no_active_games")

        if status == "stage_done":
            # 非决赛轮打完，等待管理员推进到下一阶段；active_games 通常为空。
            return Decision(action="wait", reason="stage_done_waiting_for_admin")

        # 未识别的非终态状态：保守地继续等待轮询，不主动退出。
        return Decision(action="wait", reason=f"unknown_status:{status}")


ApiCallable = Callable[[], Any]


class GameLedger:
    """对局任务生命周期表：跟踪 game_id 的 in-flight/completed 状态，防止
    ``run_tournament`` 在同一个 game_id 仍在打（或刚打完但服务端短暂还没
    更新 ``active_games``）时被重复提交给 ``on_play``。

    历史缺陷（用户复核指出）：旧实现每次看到 ``active_games`` 就整批调用
    ``on_play()``；``on_play`` 内部用线程池阻塞等待全部对局线程结束，
    这段时间之外没有任何去重机制——如果服务端在两次轮询之间仍短暂返回
    同一个 game_id（例如结算延迟、轮询与状态推进有竞态），同一局可能被
    提交给 ``on_play`` 两次。

    设计：只记录"已经提交过的 game_id 集合"（不区分 in-flight/completed，
    因为 ``on_play`` 是阻塞调用，返回时这批 game_id 已经全部处理完毕），
    每轮只把"从未见过的 game_id"筛选出来传给 ``on_play``。允许调用方在
    需要"同一 game_id 服务端标记为重开"的场景下调用 ``forget()`` 显式清除
    （例如同阶段号故障重赛，服务端会分配新的 game_id，通常不需要用到
    forget，但保留该接口用于诊断/测试）。
    """

    def __init__(self):
        self._submitted: set = set()

    def filter_new(self, game_ids: List[str]) -> List[str]:
        fresh = [gid for gid in game_ids if gid not in self._submitted]
        self._submitted.update(fresh)
        return fresh

    def forget(self, game_id: str) -> None:
        self._submitted.discard(game_id)

    @property
    def submitted_count(self) -> int:
        return len(self._submitted)


_ALREADY_READIED_409_MARKERS = ("ALREADY_READY", "TOURNAMENT_STARTED")


def _is_already_readied_409(error: ApiError) -> bool:
    """判断一个 409 ApiError 是否明确表示"已经 ready 过"（幂等成功），
    而不是其它原因的 409（例如阶段不对、并发冲突等）。只在响应体明确
    包含 ``ALREADY_READY`` 或 ``TOURNAMENT_STARTED`` 这两个已知标记时才
    返回 True——不做宽松匹配，避免把未知原因的 409 误判为幂等成功
    （未知 409 必须继续走原有重试/熔断路径，不能被静默吞掉）。"""
    body = error.body or ""
    return any(marker in body for marker in _ALREADY_READIED_409_MARKERS)


def run_tournament(
    api,
    tournament_id: Optional[str] = None,
    *,
    max_seconds: Optional[float] = None,
    poll_interval: float = 3.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_play=None,
    max_consecutive_errors: int = 20,
    max_consecutive_command_errors: int = 20,
    ledger: Optional[GameLedger] = None,
):
    """驱动 ``TournamentLifecycle`` 的真实 I/O 循环。

    - 不从 ``game_id`` 猜测赛事 ID：优先使用 ``api.me()`` 返回的
      ``tournament_id``；调用方也可显式传入已知的 ``tournament_id``
      （例如从门户报名流程获得）用于首次 ``register``。
    - 429/超时/网络断线（快照拉取失败）：捕获后退避重试，不崩溃退出；
      连续错误次数超过 ``max_consecutive_errors`` 才放弃。
    - **register/ready 命令失败**（用户复核发现的缺陷修复）：使用**独立**
      的 ``consecutive_command_errors`` 计数器，不会被"下一轮快照拉取
      成功"清零——只有命令本身成功（或 409 幂等成功）才清零。超过
      ``max_consecutive_command_errors`` 同样会抛出异常放弃，视为需要
      人工介入（例如令牌失效、服务端持续拒绝该赛事）。历史缺陷：旧实现
      两类错误共用同一个 ``consecutive_errors`` 计数器，而该计数器在每轮
      循环开头只要"快照拉取"成功就会被清零——导致即使 register/ready
      持续失败，只要 ``api.me()``/``api.tournament()`` 本身正常，熔断永远
      不会触发，程序会一直空转到 ``max_seconds`` 超时，而不是提前报错
      让人工介入。
    - **不再有默认硬截止**（用户复核发现的缺陷修复）：``max_seconds``
      默认为 ``None``（无上限），以服务器终态（``status`` 进入
      ``TERMINAL_STATUSES``）作为唯一正常退出条件，而不是"跑满 N 秒就
      放弃"——正式赛决赛圈可能顺延，硬编码时长会导致程序在赛事尚未真正
      结束时静默退出。调用方如需要一个安全阀（而非正常退出条件），应在
      外部用进程级 watchdog 监控 + 报警/重启，不应通过 ``max_seconds``
      让状态机自己"正常"退出。为向后兼容脚本化短时测试，仍可显式传入
      ``max_seconds`` 数值。
    - ``on_play(game_ids)`` 回调用于把 "play" 决定接到真正的对局线程池
      （沿用 ``bot.play_game`` 不变，本阶段不改策略）；只会收到**尚未
      提交过**的 game_id（见 ``GameLedger``），防止同一局被重复提交。
    - active_games 发现来源统一走 ``extract_active_game_ids(me, info)``
      （优先 ``info.my_games`` → ``info.active_games`` → ``me.active_games``，
      去重、忽略观战/异常项），不再只读 ``me.get("active_games")``。

    返回最终 ``Decision``（``action == "done"``）或在超时后返回
    ``Decision(action="wait", reason="timeout")``（仅当显式传入
    ``max_seconds`` 时才可能发生）。
    """
    lifecycle = TournamentLifecycle()
    game_ledger = ledger if ledger is not None else GameLedger()
    deadline = None if max_seconds is None else clock() + max_seconds
    consecutive_errors = 0
    consecutive_command_errors = 0
    _kickoff_done = [False]
    _last_state = [None]
    while deadline is None or clock() < deadline:
        try:
            me = api.me()
            tid = me.get("tournament_id") or tournament_id
            info = {}
            if tid:
                try:
                    info = api.tournament(tid) or {}
                except ApiError as error:
                    # v35：TOURNAMENT_GONE 是暂时不可达（下一轮重新查），
                    # TOURNAMENT_NOT_FOUND 才是真的没有；两者共用 404。
                    if error.status == 404:
                        info = {}
                    else:
                        raise
            snapshot = TournamentSnapshot(
                tournament_id=tid,
                status=info.get("status"),
                active_games=extract_active_game_ids(me, info),
                qualified=info.get("qualified"),
                qualify_role=info.get("qualify_role"),
                raw_me=me,
                raw_tournament=info,
            )
            consecutive_errors = 0
        except (ApiError, OSError, TimeoutError) as error:
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                raise
            sleep(min(poll_interval * consecutive_errors, 30.0))
            continue

        # 2026-09-24 实盘教训（错过「应牌友要求的四测」阶段1，101 人报名的海选）：
        # 官方接入指南 §三 的最小 Bot 在**启动时无条件**先 register 再 ready、
        # 并把 409 打印出来；而 lifecycle.step() 只在 status is None 时 register、
        # 只在 registering/stage_open 时 ready。于是 bot 若在开赛后才启动
        # （当天 08:00 开赛、08:13 才起进程），两条路都不触发：
        # my_registered=true / my_ready=false / stage_status=running / my_games=[]，
        # 而进程**一个字都不打印**，肉眼无法区分「正常等待」和「这一阶段已经废了」。
        # 指南 §2.4 明确：running/stage_done/finished/void 期间提交 ready → 409
        # TOURNAMENT_STARTED，所以补发注定失败——但**必须让人当场看到**。
        # 另见指南 §2.6：ready 只认「开赛时刻 ≤90s 内有已认证请求者」，
        # 所以开赛前进程就得在跑，事后无法补救。
        # 收窄触发条件：只在「已开赛 / 阶段间隙，且本方未出席、且无对局」时补发一次。
        # 这正是 lifecycle 两条路都不触发的死角；其余状态由 lifecycle 正常处理，
        # 不额外发请求（否则会改变既有 register/ready 调用次数语义）。
        # 只认 running。**stage_done 不算死角**——阶段之间等管理员推进时
        # my_ready=false / 活跃场=0 是完全正常的（ready 要等 stage_open 才该提交），
        # 把它算进来会误报「上不了场」并白发一次注定 409 的请求。
        _stuck = (snapshot.status == "running"
                  and not info.get("my_ready")
                  and not snapshot.active_games)
        if _stuck and not _kickoff_done[0]:
            _kickoff_done[0] = True
            if tid:
                for name in ("register", "ready"):
                    try:
                        getattr(api, name)(tid)
                        print("[tournament] 启动自检 %s: OK" % name)
                    except ApiError as error:
                        print("[tournament] 启动自检 %s 被拒: HTTP %s %s"
                              % (name, error.status, error.code or ""))
                    except (OSError, TimeoutError) as error:
                        print("[tournament] 启动自检 %s 网络错误: %r" % (name, error))

        # 状态可见性：只在变化时打印，不刷屏。原先整个轮询循环零输出，
        # 这是 2026-09-24 那次「盯着空白终端 20 分钟」的直接原因。
        state_key = (snapshot.status, info.get("stage_status"),
                     bool(info.get("my_ready")), bool(snapshot.qualified),
                     len(snapshot.active_games or []))
        if state_key != _last_state[0]:
            _last_state[0] = state_key
            _status_zh = {
                None: "尚未发现赛事", "": "尚未发现赛事",
                "registering": "报名期", "running": "比赛进行中",
                "stage_done": "本阶段已打完，等管理员推进下一阶段",
                "stage_open": "下一阶段确认出席期",
                "finished": "赛事已结束", "closed": "赛事已关闭", "void": "赛事作废",
            }
            stage = (info.get("stage") or {})
            parts = [_status_zh.get(snapshot.status, str(snapshot.status))]
            if stage.get("no"):
                parts.append("第%s/%s阶段" % (stage.get("no"), stage.get("total")))
            parts.append("出席：%s" % ("已确认" if info.get("my_ready") else "未确认"))
            if snapshot.active_games:
                parts.append("进行中对局 %d 场" % len(snapshot.active_games))
            print("[赛事] " + "｜".join(parts))
            if snapshot.status in (None, ""):
                print("       等待赛事出现，进程保持在线（这是正常状态）")
            if snapshot.status == "stage_done":
                print("       阶段之间的正常等待，出席要等下一阶段开放后再确认，进程别关")
            if (snapshot.status == "running"
                    and not info.get("my_ready") and not snapshot.active_games):
                print("       ❌ 已开赛但我们未确认出席，也没有对局 —— 本阶段上不了场了。")
                print("          ready 只在开赛前受理（之后一律 409 TOURNAMENT_STARTED）。")
                print("          下次请在开赛前跑 tools/tourney_guard.py，确认「出席=True」再开赛。")

        decision = lifecycle.step(snapshot)
        if decision.action == "register":
            try:
                if tid:
                    api.register(tid)
                # 只有 API 调用未抛异常（即服务端确认成功）才提交状态；
                # 若 tid 尚不可用，本轮不提交，下一轮 step() 会再次尝试。
                if tid:
                    lifecycle.confirm_registered()
                    consecutive_command_errors = 0
            except ApiError as error:
                if error.status == 409:
                    # 已报名（重复报名）视为幂等成功，提交状态，停止重试。
                    lifecycle.confirm_registered()
                    consecutive_command_errors = 0
                else:
                    # 非 409 错误：不提交状态，下一轮 step() 会再次返回
                    # register 决定，从而重试（P0 修复点）。用独立计数器
                    # 累计，不会被下一轮快照拉取成功清零（本次修复点）。
                    consecutive_command_errors += 1
                    if consecutive_command_errors >= max_consecutive_command_errors:
                        raise
        elif decision.action == "ready":
            try:
                api.ready(tid)
                # 只有确认成功才提交"已确认出席"状态；失败则不提交，
                # 下一轮 step() 在相同状态下会再次返回 ready 决定，
                # 从而重试，而不是被永久跳过（P0 修复点）。
                lifecycle.confirm_readied()
                consecutive_command_errors = 0
            except ApiError as error:
                if error.status == 409 and _is_already_readied_409(error):
                    # 409 且明确包含 ALREADY_READY/TOURNAMENT_STARTED：视为
                    # 幂等成功（服务端已经认可了这次出席，或阶段已经开赛）
                    # ——提交"已确认出席"状态，清零命令错误计数器，不再重试。
                    lifecycle.confirm_readied()
                    consecutive_command_errors = 0
                else:
                    # 其它 409（原因未知/无法识别）与非 409 错误（含 401/403
                    # 等真实错误）保持原重试/熔断行为：不提交状态，不吞掉
                    # 错误——下一轮 step() 在相同状态下会再次返回 ready 决定
                    # 重试；用独立计数器累计，不会被下一轮快照拉取成功清零；
                    # 超过阈值仍会抛出异常放弃（视为需要人工介入）。
                    consecutive_command_errors += 1
                    if consecutive_command_errors >= max_consecutive_command_errors:
                        raise
        elif decision.action == "play":
            if on_play:
                fresh_ids = game_ledger.filter_new(decision.game_ids)
                if fresh_ids:
                    on_play(fresh_ids)
        elif decision.action == "done":
            return decision
        sleep(poll_interval)
    return Decision(action="wait", reason="timeout")
