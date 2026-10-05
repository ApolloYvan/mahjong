"""决策可观测性支持：schema 版本、决策 ID、配置/状态哈希、采样策略。

阶段2目标（服务端结算账和可复现观测）的基础设施。设计原则：
- 服务端 ``round_ended`` 的 winner/fan/detail/scores/next_dealer 是结算真相；
  本地 ``rules.evaluate`` 计算结果只能标记为 ``local_estimate``，不能混淆为
  已确认结果（独立技术评审报告已核验的 fan4 vs fan2 差异正是源于把本地估算
  当成了事实）。
- 每次决策需要能被复现比对：``decision_id`` 唯一标识一次决策，``state_hash``
  是规范化状态的确定性哈希（同一状态输入必须产生同一哈希，便于跨会话去重/
  比对），``config_hash`` 标识当次生效的规则配置。
- 日志不得阻塞动作线程：本模块的哈希计算是纯 CPU 本地操作（无 I/O），
  数量级是毫秒级以内，不引入网络或磁盘依赖；采样决策函数是纯函数，
  由调用方决定何时执行完整记录 vs 跳过。
"""
import hashlib
import json
import os
import random
import uuid

SCHEMA_VERSION = 2
# 日志 schema 版本（决策 JSONL 记录里的 schema_version 字段）与
# mj.datastore.SCHEMA_VERSION（SQLite datastore 的表结构版本）是两个独立
# 概念，不强制数值相同：前者描述"日志记录本身携带哪些字段"（v1 -> v2
# 新增 decision_attempt/decision_attempt_outcome/build_hash/B3 事件），
# 后者描述"SQLite 表结构版本"。旧日志 schema_version=1 仍可被
# mj.data_import 正常导入（缺失字段按既有 UNKNOWN_LEGACY/
# BUILD_HASH_UNAVAILABLE 等哨兵处理，不强制升级）。

# 策略版本标签：legacy = 阶段1修复前就存在的评分核心（strategy.py/ev.py），
# 阶段4引入统一规划器后，这里会区分 legacy vs planner_v1 等取值。
POLICY_VERSION = "legacy"

_BUILD_HASH_CACHE = None


def build_hash(explicit: str = None) -> str:
    """构建标识哈希：用于把日志/导入/SQLite/summary/fixture-export 中的一次
    决策绑定到"当时运行的是哪一份代码"，供事后区分"策略变化导致的行为
    差异"与"同一份代码的正常波动"。

    解析优先级：
    1. 显式传入的 ``explicit`` 参数；
    2. 环境变量 ``MJ_BUILD_HASH``（CI/发布流程注入的构建号/commit sha，
       优先级最高，跨进程/跨机器稳定）；
    3. 退化方案：对 ``mj/`` 包内全部 ``.py`` 文件内容做确定性 SHA-256
      （本仓库当前没有初始化 git，不能依赖 ``git rev-parse``；对源码内容
      整体做确定性哈希，同一份代码总是产生同一个 build_hash，代码变化后
      哈希也会随之变化，不依赖外部工具，可离线计算）。

      P1 返修：哈希输入只使用相对于 ``mj`` 包根目录的规范化相对路径 +
      文件内容——不再把绝对路径本身喂入哈希（旧实现的 bug：同一份源码
      复制到两个不同绝对目录下，会因为路径前缀不同而产生不同
      build_hash，导致"同一份代码"被误判为"不同构建"）。先一次性收集
      全部 ``.py`` 相对路径，再整体排序后逐个更新哈希，不依赖
      ``os.walk`` 的目录遍历顺序（不同文件系统/平台下 os.walk 的目录
      顺序不保证一致）。

    结果在进程内缓存（退化方案需要遍历包目录，避免每次决策都重新计算）。
    """
    global _BUILD_HASH_CACHE
    if explicit:
        return explicit
    env_value = os.environ.get("MJ_BUILD_HASH")
    if env_value:
        return env_value
    if _BUILD_HASH_CACHE is not None:
        return _BUILD_HASH_CACHE
    package_dir = os.path.dirname(os.path.abspath(__file__))
    relative_paths = []
    for root, _dirs, files in os.walk(package_dir):
        for name in files:
            if not name.endswith(".py"):
                continue
            abs_path = os.path.join(root, name)
            rel_path = os.path.relpath(abs_path, package_dir).replace(os.sep, "/")
            relative_paths.append(rel_path)
    relative_paths.sort()  # 整体排序，避免 os.walk 遍历顺序影响哈希结果
    digest = hashlib.sha256()
    for rel_path in relative_paths:
        abs_path = os.path.join(package_dir, rel_path.replace("/", os.sep))
        try:
            with open(abs_path, "rb") as handle:
                digest.update(rel_path.encode("utf-8"))
                digest.update(handle.read())
        except OSError:
            continue
    _BUILD_HASH_CACHE = digest.hexdigest()[:16]
    return _BUILD_HASH_CACHE


def new_decision_id() -> str:
    """生成本次决策的唯一 ID，供跨日志条目（decision/hu_detail/action_rejected/
    fallback_sent/round_end）关联同一次决策。"""
    return uuid.uuid4().hex


def _stable_json(value) -> str:
    """确定性 JSON 序列化：排序键、无空白差异，保证同一逻辑内容产生同一哈希。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)


def config_hash(rules: dict) -> str:
    """规则配置哈希：标识"当次生效的 config"，用于事后判断两次决策是否
    在同一套规则参数下产生（避免误把"配置变了导致行为不同"当成策略 bug）。"""
    return hashlib.sha256(_stable_json(rules or {}).encode("utf-8")).hexdigest()[:16]


_STATE_HASH_FIELDS = (
    "seat", "phase", "turn", "hand", "drawn_tile", "dealer", "wall_remaining",
    "discards", "melds_all", "responding_seats", "chain_count", "piao", "round_no",
)


def state_hash(state) -> str:
    """规范状态哈希：对 DecisionState 的关键字段做确定性哈希。

    只取 ``_STATE_HASH_FIELDS`` 列出的字段，不取整个 raw snapshot——服务端
    快照可能包含时间戳、序号等每次轮询都会变化但与"决策相关的状态"无关的
    字段，如果全量哈希会导致同一逻辑状态产生不同哈希，失去去重/比对意义。
    """
    payload = {name: getattr(state, name) for name in _STATE_HASH_FIELDS}
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()[:16]


def should_log_full(*, is_abnormal: bool, sample_rate: float = 0.05,
                    rng: random.Random = None) -> bool:
    """采样决策：异常/冲突/非法动作/高延迟全量记录；正常状态按 ``sample_rate``
    低频随机采样，但采样出的记录本身必须能够重建"正常分母"（调用方仍需
    对全部决策计数，只是不必把每条都写全量payload——这由调用方在计数器里
    单独维护，本函数只回答"这一条要不要写完整payload"）。
    """
    if is_abnormal:
        return True
    rng = rng or random
    return rng.random() < sample_rate


def stable_sample_state(state_hash: str, *, sample_rate: float = 0.05) -> bool:
    """B3 返修：正常 state 使用**稳定采样**而不是 ``random.random()``——
    同一个 ``state_hash`` 在同一 ``sample_rate`` 下，采样结果必须是确定性
    的、可复现的（不依赖调用顺序/进程内随机数状态），否则"哪些正常状态被
    采样进日志"这件事本身不可复现，调试时无法用同一个状态哈希去反查。

    实现：把 ``state_hash`` 的 SHA-256 摘要的前 8 位十六进制数解释成
    [0, 1) 区间的一个确定性伪随机数，与 ``random.random() < sample_rate``
    的比较方式完全一致，只是随机源换成了哈希值本身。
    """
    if not state_hash:
        return False
    digest_int = int(hashlib.sha256(state_hash.encode("utf-8")).hexdigest()[:8], 16)
    fraction = digest_int / 0xFFFFFFFF
    return fraction < sample_rate


def local_estimate_marker() -> dict:
    """本地规则计算结果的来源标记，明确区分于服务端结算真相。"""
    return {"source": "local_estimate"}


def server_truth_marker() -> dict:
    """服务端 round_ended 结算结果的来源标记（结算真相）。"""
    return {"source": "server_truth"}


def server_truth_unavailable_marker() -> dict:
    """B4：当前生产 API（``mj/api.py``）在对局在线阶段不提供完整的
    ``round_ended`` 结算事件（这类事件只能通过赛后 ``tools/pull_all_events.py``
    拉取门户事件流获得，见 ``mj/reconcile.py:server_truths_from_portal_game``）。
    在线记录 ``hu_detail``（local_estimate）时，必须显式标记"服务端结算
    在本次在线会话中不可得"，而不是发明一个假的 server_truth 或者干脆不
    提及这件事——调用方（`mj/bot.py`）据此可以诚实地告诉下游"这条记录
    需要赛后对账补齐，不能当作已经交叉验证过的结果"。
    """
    return {"server_truth_unavailable": True,
            "server_truth_note": "online API does not expose round_ended; "
                                  "reconcile via post-match portal events import"}
