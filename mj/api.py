import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .sanitize import sanitize_text


class ApiError(Exception):
    """P0-3 返修：``body`` 是服务端原始响应文本，可能回显请求里的
    Authorization/Bearer 头或其它敏感字段（网关错误页/调试信息常见做法）。
    异常消息（``str(error)``，被 ``mj/bot.py``/``mj/logging.py`` 到处用
    ``str(error)``/``repr(error)`` 落盘和打印）必须先经 ``sanitize_text``
    脱敏——不能只在导入阶段（``mj/data_import.py``）才脱敏，那时敏感文本
    已经在源头（JSONL 文件、stdout/stderr）明文出现过一次。``self.body``
    仍保留脱敏后的文本供调用方读取，不再暴露未脱敏的原始值。
    """

    def __init__(self, status, body):
        safe_body = sanitize_text(body) if body is not None else body
        super().__init__(f"HTTP {status}: {safe_body}")
        self.status = status
        self.body = safe_body

    @property
    def code(self):
        """服务端 body 里的 ``code`` 字段，取不到时返回 ""。

        接入指南 v35 起，**同一个 HTTP 状态码下语义相反的错误必须靠 code 区分**：
        404 既可能是 ``TOURNAMENT_GONE``（房暂时不可达，应重试）也可能是
        ``TOURNAMENT_NOT_FOUND``（房不存在，应放弃）。只看 ``status == 404``
        会把该重试的当成该放弃的——实测 2026-09-23 房间 a_e2eb6e654532 的 7 张桌
        各收到正好 10 次 404 后被整桌放弃（见 docs/audit/STALL_2026-09-23.md）。
        """
        body = self.body or ""
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and isinstance(parsed.get("code"), str):
                return parsed["code"]
        except ValueError:
            pass
        match = re.search(r'"code"\s*:\s*"([A-Z_]+)"', body)
        return match.group(1) if match else ""


# 接入指南 v35：404 下语义相反的两类错误，必须按 code 判型，不能只看 status。
TRANSIENT_CODES = frozenset({"TOURNAMENT_GONE"})
PERMANENT_CODES = frozenset({"TOURNAMENT_NOT_FOUND", "GAME_NOT_FOUND",
                             "FEATURE_DISABLED", "PORTAL_BINDING_REQUIRED"})


def is_transient(error):
    """404/5xx 是否属于「应重试」。未知 code 保守按**可重试**处理——
    放弃一局的代价（整桌被服务端托管）远高于多轮询几次。"""
    code = getattr(error, "code", "")
    if code in TRANSIENT_CODES:
        return True
    if code in PERMANENT_CODES:
        return False
    return True


class MahjongApi:
    # 令牌级全局限速：10 个对局线程共用一个用户配额（/state 16/s/user），
    # 超限触发 429 重试链（0.4s backoff）会把 state 延迟抬到 422ms×k——
    # 回合窗只有 ~2s，超时即被服务器托管（s44 幽灵南事故）。发请求前先排槽位。
    _pacers = {}
    _pacer_guard = threading.Lock()
    DEFAULT_PACE_INTERVAL = 1.0 / 14.0
    count_429 = 0

    def __init__(self, server, token, timeout=35, verify_tls=False, pace_interval=None):
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
        parts = urlsplit(self.server)
        self._host = parts.hostname
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._https = parts.scheme == "https"
        self._local = threading.local()
        if pace_interval is None:
            pace_interval = self.DEFAULT_PACE_INTERVAL
        self.pace_interval = pace_interval
        key = (self.server, token)
        with self._pacer_guard:
            pacer = self._pacers.get(key)
            if pacer is None:
                pacer = {"lock": threading.Lock(), "next_slot": 0.0}
                self._pacers[key] = pacer
        self._pacer = pacer

    def _pace(self):
        """按令牌全局排槽位：槽位在锁内预留、锁外睡眠，线程间近似公平。"""
        if self.pace_interval <= 0:
            return
        with self._pacer["lock"]:
            now = time.monotonic()
            slot = max(now, self._pacer["next_slot"])
            if slot - now > self.pace_interval * 8:
                slot = now
            self._pacer["next_slot"] = slot + self.pace_interval
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._https:
                conn = http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout, context=self.context)
            else:
                conn = http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)
            self._local.conn = conn
        return conn

    def request(self, method, path, body=None, retries=3, timeout=None, backoff=2.0, pace=False):
        """P0 修复：限速（``_pace()``）默认**不**生效，只有显式传入
        ``pace=True`` 的调用点才排队等槽位——目前只有 ``state()``（每令牌
        16 次/秒的 ``GET /api/games/{id}/state`` 官方限制）显式传入
        ``pace=True``。

        修复前的 bug：``_pace()`` 无条件作用于全部 HTTP 请求（含
        ``POST /api/games/{id}/action``），导致 10 场并发下 state 轮询
        占满槽位后，动作请求（回合窗口通常只有 ~2s）被同一个槽位队列
        延后发送，真实延迟因此被人为抬高。``action()``/``match()``/
        ``me()``/``rules()``/``register()``/``ready()``/``tournament()``
        等低频或动作关键路径的调用一律不占用 state 槽位，保留各自独立的
        429/网络重试规则（``retries``/``backoff`` 参数不受影响）。
        """
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        attempt = 0
        while True:
            try:
                if pace:
                    pace_started = time.monotonic()
                    self._pace()
                    # 2026-10-05 诊断：排队等待（本地限速）与 HTTP 往返分开记，见 docs/EXPERT_Q_LATENCY.md
                    self._local.last_pace_ms = (time.monotonic() - pace_started) * 1000
                conn = self._connection()
                conn.timeout = timeout if timeout is not None else self.timeout
                conn.request(method, path, body=data, headers=headers)
                response = conn.getresponse()
                raw = response.read().decode("utf-8")
                if response.status == 429:
                    MahjongApi.count_429 += 1   # 诊断计数（进程内累计），随 state_request_metric 落盘
                if response.status == 429 and attempt < retries:
                    attempt += 1
                    time.sleep(backoff)
                    continue
                if response.status >= 400:
                    raise ApiError(response.status, raw)
                return json.loads(raw) if raw else {}
            except ApiError:
                raise
            except (TimeoutError, socket.timeout):
                # Python 3.9：socket.timeout 不是 TimeoutError（3.10 起才合并）。
                # 只写 `except TimeoutError` 时，本机上 socket 超时会落到下面的
                # OSError 分支被重试 retries 次 —— state() 是 retries=8，单次
                # 调用最坏阻塞 8×timeout；action() 是 retries=2，最坏 3×35=105 秒，
                # 而响应窗口只有约 2 秒。实测后果：2026-09-23 的连续对局里出现
                # 四次 11~17 分钟的整进程停滞，期间服务端替我们打掉 69~73% 的牌，
                # 两个房间因此报废（见 docs/audit/STALL_2026-09-23.md）。
                conn = getattr(self._local, "conn", None)
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
                    self._local.conn = None
                raise
            except (http.client.HTTPException, OSError) as error:
                self._local.conn = None
                if attempt < retries:
                    attempt += 1
                    if attempt > 1:
                        time.sleep(min(backoff, 1.0))
                    continue
                raise ApiError(0, str(error)) from error

    def me(self):
        return self.request("GET", "/api/me")

    def register(self, tournament_id):
        """报名正式赛事：``POST /api/tournaments/{id}/register``。
        历史上 api.py 缺这个方法（HANDOFF §1.3 已记录），导致正式赛路径
        只能靠人工在门户点「报名」按钮完成，无法全自动。"""
        return self.request("POST", f"/api/tournaments/{tournament_id}/register", {})

    def ready(self, tournament_id=None):
        """确认出席：接入指南权威路径是 ``POST /api/tournaments/{tid}/ready``
        （按 tournament_id 定向）。``tournament_id`` 缺省时保留旧版
        ``POST /api/tournaments/me/ready`` 兼容入口（向后兼容调用方尚未
        传入 tid 的场景），不在同一次调用里重复请求两个端点。"""
        if tournament_id:
            return self.request("POST", f"/api/tournaments/{tournament_id}/ready", {})
        return self.request("POST", "/api/tournaments/me/ready", {})

    def tournament(self, tournament_id):
        return self.request("GET", "/api/tournaments/" + tournament_id)

    def rules(self):
        return self.request("GET", "/api/tournaments/me/rules")

    def match(self, limits=None):
        return self.request("POST", "/api/match", limits or {})

    def state(self, game_id, seq=0, timeout=None):
        """限速端点：符合每令牌 16 次/秒官方限制，走 ``_pace()`` 排槽位——
        这是 P0 修复后**唯一**显式传入 ``pace=True`` 的调用点。不得通过
        缩短本地 ``pace_interval``/绕过限速来解决 action 延迟问题（任务
        书明确要求：不得突破官方 state 频率限制）。"""
        path = f"/api/games/{game_id}/state?seq={seq}"
        return self.request("GET", path, timeout=timeout, retries=8, backoff=0.4, pace=True)

    def notify(self, game_id):
        """SSE 唤醒流。

        2026-09-23 实测：本文件里 ``state``/``action`` 走 ``http.client``（不读
        ``HTTP_PROXY`` 环境变量，直连，实测 p50 4~17ms），而 ``notify`` 原来用
        ``urllib.request.urlopen``——**默认会读环境代理**。实测
        ``proxy_bypass("10.240.169.190") is False``，即赛事服务器这个内网地址
        的 SSE 长连接被塞进了本机代理（Clash Verge，127.0.0.1:7897），而决策
        请求没有。这个不一致是无意造成的，不是设计。

        代理转发 SSE 的典型后果与实测现象一一对应：缓冲流式响应 → 唤醒延迟 →
        退化成 5 秒 watchdog 轮询，而响应窗口只有约 2 秒；实测"我方手上确有
        两张、能碰"的 2915 个窗口里 **19.8%（576 次）因超时完全没反应**。
        另见 docs/audit/STALL_2026-09-23.md 里 899 秒（= 15 分钟整，典型代理
        空闲超时）的停滞。

        因此这里显式使用一个禁用代理的 opener，让 notify 与 state/action 走
        同一条路。若将来确实需要经代理访问赛事服务器，改这里而不是依赖
        环境变量。
        """
        request = urllib.request.Request(self.server + f"/api/games/{game_id}/notify")
        request.add_header("Authorization", "Bearer " + self.token)
        return self._direct_opener().open(request, timeout=self.timeout)

    @classmethod
    def _direct_opener(cls):
        opener = getattr(cls, "_direct", None)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=cls._shared_context()),
            )
            cls._direct = opener
        return opener

    @classmethod
    def _shared_context(cls):
        ctx = getattr(cls, "_ctx", None)
        if ctx is None:
            ctx = ssl._create_unverified_context()
            cls._ctx = ctx
        return ctx

    def action(self, game_id, action, tile="", tiles=None):
        body = {"action": action, "tile": tile}
        if tiles is not None:
            body["tiles"] = tiles
        return self.request("POST", f"/api/games/{game_id}/action", body, retries=2, backoff=0.2)

    def version(self):
        request = urllib.request.Request(self.server + "/portal/api/guide/version")
        with urllib.request.urlopen(request, timeout=10, context=self.context) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def test_events(server, room_id, batch):
        request = urllib.request.Request(
            f"{server.rstrip('/')}/api/test-rooms/{room_id}/games/{batch}/events"
        )
        with urllib.request.urlopen(request, timeout=35) as response:
            return json.loads(response.read().decode("utf-8"))
