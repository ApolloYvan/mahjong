import gzip
import json
import os
import shutil
import time
from datetime import datetime, timezone

from .observability import (
    SCHEMA_VERSION,
    POLICY_VERSION,
    build_hash as _build_hash,
    config_hash as _config_hash,
    local_estimate_marker,
    new_decision_id,
    state_hash as _state_hash,
)
from .sanitize import get_salt, sanitize
from .state import normalize as _normalize_state


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class DecisionLog:
    """B2 返修：支持按天（既有行为）+ 按大小轮转，轮转后的旧文件自动
    gzip 压缩归档（不阻塞主写入路径——压缩发生在轮转触发的那次 append
    调用内，属于低频操作，可接受偶尔多花几毫秒；不引入后台压缩线程以
    保持实现简单，压缩失败时静默保留未压缩的 .jsonl 文件，不影响主日志
    功能）。"""

    def __init__(self, directory="logs", max_bytes=None, gzip_on_rotate=True):
        self.directory = directory
        self.max_bytes = max_bytes  # None = 不做按大小轮转，仅按天轮转（既有行为）
        self.gzip_on_rotate = gzip_on_rotate
        os.makedirs(directory, exist_ok=True)

    def _current_path(self):
        return os.path.join(self.directory, datetime.now().strftime("%Y-%m-%d") + ".jsonl")

    def _maybe_rotate(self, path):
        if not self.max_bytes:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < self.max_bytes:
            return
        # 找一个不冲突的轮转文件名：<base>.1.jsonl, <base>.2.jsonl, ...
        base = path[:-len(".jsonl")] if path.endswith(".jsonl") else path
        n = 1
        while True:
            rotated = f"{base}.{n}.jsonl"
            if not os.path.exists(rotated) and not os.path.exists(rotated + ".gz"):
                break
            n += 1
        try:
            os.rename(path, rotated)
        except OSError:
            return
        if self.gzip_on_rotate:
            try:
                with open(rotated, "rb") as src, gzip.open(rotated + ".gz", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.remove(rotated)
            except OSError:
                pass  # 压缩失败不影响主日志功能，保留未压缩的轮转文件

    def append(self, kind, payload):
        """P0-3 返修：这是所有落盘路径（同步 ``DecisionLog`` 直接调用，以及
        ``AsyncDecisionLog`` 写线程调用 ``self._inner.append()``）唯一收敛
        的源头写入点——payload 在写入 JSONL 之前统一经过 ``mj.sanitize``
        脱敏，不依赖调用方（``mj/bot.py``/``mj/async_log.py``）各自记得
        脱敏、也不依赖\"导入阶段才脱敏\"（旧版只在 ``mj/data_import.py``
        入库前脱敏，原始 JSONL 文件本身、轮转/压缩后的历史文件都仍是明文，
        一旦泄露这些文件本身就已经泄露）。

        ``sanitize()`` 在配置了 ``MJ_SANITIZE_SALT`` 环境变量时会把
        session_id/user_id/nickname 等标识符替换为带盐稳定假 ID（保留分析
        关联能力）；未配置盐时退化为整体 REDACTED（安全默认）。凭据类字段
        （Authorization/token/cookie/password 等）无论是否有盐，一律整体
        REDACTED。"""
        path = self._current_path()
        self._maybe_rotate(path)
        safe_payload = sanitize(payload, salt=get_salt())
        record = {"time": utc_now(), "kind": kind, "payload": safe_payload}
        try:
            with open(path, "a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # 日志是旁路：文件被外部短暂锁定（同步盘/杀软）时丢弃本条，绝不炸对局线程
            pass

    def action(self, game_id, snapshot, decision, client_prepare_ms=None, action_request_ms=None,
              opp_chain=False, rules=None, decision_id=None, mc=None):
        """记录一次决策。阶段2新增可复现观测字段（不影响原有字段）：
        - schema_version / policy_version：日志格式与当前生效策略版本标签；
        - decision_id：本次决策唯一 ID，未显式传入时自动生成，供跨条目
          （action_rejected/fallback_sent/hu_detail）关联同一次决策；
        - config_hash：生效规则配置的哈希（rules 缺省时退化为空字典的哈希）；
        - state_hash：规范化决策状态（DecisionState）的哈希，便于跨会话去重/
          对比同一逻辑状态是否被多次记录；
        - build_hash（P1-2）：标识当前运行的代码版本，贯穿日志/导入/SQLite/
          summary/fixture-export，供事后区分"策略变化"与"同一份代码的正常
          波动"。
        """
        decision_id = decision_id or new_decision_id()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "decision_id": decision_id,
            "game_id": game_id,
            "seat": snapshot.get("seat"),
            "round_no": snapshot.get("round_no"),
            "dealer": snapshot.get("dealer"),
            "phase": snapshot.get("phase"),
            "opp_chain": bool(opp_chain),
            "wall_remaining": snapshot.get("wall_remaining"),
            "scores": snapshot.get("scores"),
            "drawn_tile": snapshot.get("drawn_tile"),
            "seq": snapshot.get("seq"),
            "turn": snapshot.get("turn"),
            "responding_seats": snapshot.get("responding_seats"),
            "hand": snapshot.get("my_hand"),
            "melds": snapshot.get("melds"),
            "rules": snapshot.get("rules") or {},
            "piao": snapshot.get("piao", 0),
            "god": snapshot.get("god"),
            "decision": decision,
            "config_hash": _config_hash(rules if rules is not None else snapshot.get("rules")),
            "build_hash": _build_hash(),
        }
        state = _normalize_state(snapshot, rules=rules)
        if state is not None:
            payload["state_hash"] = _state_hash(state)
        if mc:
            payload["mc"] = mc   # 实时 MC 摘要（只在 MC 实际参与这次决策时才有这个键）
        if client_prepare_ms is not None:
            payload["client_prepare_ms"] = round(client_prepare_ms, 3)
        if action_request_ms is not None:
            payload["action_request_ms"] = round(action_request_ms, 3)
        self.append("decision", payload)
        return decision_id

    def decision_attempt(self, game_id, *, decision_id, round_no=None, seat=None, state_hash=None,
                          action=None, tile=None, policy_version=None, schema_version=None,
                          config_hash=None, build_hash=None, attempted_at=None):
        """P0-1：在调用 ``api.action()`` 之前记录一次"尝试意图"，独立于
        最终结果。必须包含 decision_id/game_id/state_hash/action/tile/
        policy_version/schema_version/config_hash/build_hash。"""
        self.append("decision_attempt", {
            "decision_id": decision_id,
            "game_id": game_id,
            "round_no": round_no,
            "seat": seat,
            "state_hash": state_hash,
            "action": action,
            "tile": tile,
            "policy_version": policy_version if policy_version is not None else POLICY_VERSION,
            "schema_version": schema_version if schema_version is not None else SCHEMA_VERSION,
            "config_hash": config_hash,
            "build_hash": build_hash if build_hash is not None else _build_hash(),
            "outcome": "pending",
        })

    def decision_attempt_outcome(self, *, decision_id, outcome, outcome_detail=None,
                                  resolved_at=None, game_id=None):
        """P0-1：用与对应 ``decision_attempt`` 相同的 decision_id 记录最终
        结果（success/timeout/api_error/conflict_409/fallback_sent/hu）。

        P1 修复：``game_id`` 为可选补充字段——历史日志（本字段引入之前
        写入的记录）没有这个字段仍可正常导入/分析（mj.data_import 按
        字典 get 读取，缺失时为 None，不视为错误）；新写入的记录携带
        ``game_id`` 后，``mj.timing`` 可以不必再依赖跨表 join 才能把
        outcome 记录关联回具体对局。
        """
        self.append("decision_attempt_outcome", {
            "decision_id": decision_id,
            "game_id": game_id,
            "outcome": outcome,
            "outcome_detail": outcome_detail,
        })

    def piao_attempt(self, game_id, snapshot, decision_id=None):
        self.append("piao_attempt", {
            "decision_id": decision_id,
            "game_id": game_id,
            "seat": snapshot.get("seat"),
            "round_no": snapshot.get("round_no"),
            "dealer": snapshot.get("dealer"),
            "phase": snapshot.get("phase"),
            "hand": snapshot.get("my_hand"),
            "melds": snapshot.get("melds"),
            "piao": snapshot.get("piao", 0),
            "god": snapshot.get("god"),
            "chain_count": snapshot.get("chain_count", 0),
        })

    def action_rejected(self, game_id, phase, decision, error, count, snapshot, decision_id=None):
        self.append("action_rejected", {
            "decision_id": decision_id,
            "game_id": game_id,
            "seat": snapshot.get("seat"),
            "round_no": snapshot.get("round_no"),
            "phase": phase,
            "decision": decision,
            "rejections": count,
            "error": str(error),
            "wall_remaining": snapshot.get("wall_remaining"),
            "hand": snapshot.get("my_hand"),
            "drawn_tile": snapshot.get("drawn_tile"),
            "melds": snapshot.get("melds"),
        })

    def fallback_sent(self, game_id, snapshot, tile, accepted, error=None, decision_id=None):
        self.append("fallback_sent", {
            "decision_id": decision_id,
            "game_id": game_id,
            "seat": snapshot.get("seat"),
            "round_no": snapshot.get("round_no"),
            "tile": tile,
            "accepted": bool(accepted),
            "error": None if error is None else str(error),
            "hand": snapshot.get("my_hand"),
            "drawn_tile": snapshot.get("drawn_tile"),
        })

    def notify(self, game_id, payload):
        self.append("notify", {
            "game_id": game_id,
            "seq": payload.get("seq"),
            "closed": bool(payload.get("closed")),
        })

    def state(self, game_id, requested_seq, response, elapsed_ms):
        self.append("state", {
            "game_id": game_id,
            "requested_seq": requested_seq,
            "response_seq": response.get("seq"),
            "pending": bool(response.get("pending")),
            "gap": bool(response.get("gap")),
            "event_count": len(response.get("events") or []),
            "state_request_ms": round(elapsed_ms, 3),
        })

    def state_request_metric(self, *, game_id, requested_seq, returned_seq, trigger,
                             elapsed_ms, pending=False, gap=False, state_hash=None):
        """P1 修复：notify 驱动状态获取的紧凑度量——不记录完整快照，只记
        录足以统计 notify 利用率/seq gap/watchdog 次数的字段。``trigger``
        取值 'notify'/'watchdog'/'recovery'，分别对应"由 notify 触发的
        增量请求"/"低频全量兜底"/"首次进入或检测到 gap 后的全量恢复"。"""
        self.append("state_request_metric", {
            "game_id": game_id,
            "requested_seq": requested_seq,
            "returned_seq": returned_seq,
            "trigger": trigger,
            "elapsed_ms": round(elapsed_ms, 3),
            "pending": bool(pending),
            "gap": bool(gap),
            "state_hash": state_hash,
        })

    def round_end(self, game_id, result):
        self.append("round_end", {"game_id": game_id, "result": result})

    def result(self, game_id, state):
        self.append("result", {"game_id": game_id, "state": state})

    def error(self, game_id, error):
        self.append("error", {"game_id": game_id, "error": str(error)})
