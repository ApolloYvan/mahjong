"""统一脱敏入口：所有对外输出（errors 摘要、CLI 输出、fixture-export、
异步日志异常文本）必须经过本模块，不得各自实现脱敏逻辑（历史教训：
data_import._redact_summary 只做截断不做替换，导致 64 位令牌/Bearer 头/
action_rejected.payload.error 中的 token 原样进入数据库和终端输出——
详见 SONNET5_DATA_REVIEW.md P0"脱敏实现会泄露令牌"）。

两类能力：
1. ``sanitize()``/``sanitize_text()``：递归/字符串级脱敏——删除或替换
   Authorization/Bearer/token/cookie/session/majiang_sid/密码类字段，以及
   任意位置出现的历史 64 位十六进制令牌样式（不依赖字段名）。
2. ``stable_pseudo_id()``：带域前缀的 HMAC-SHA256 稳定假 ID，用于外发场景
   替换 user_id/昵称/game_id 等标识符。盐必须来自环境变量或显式参数，
   绝不能被写入输出（``get_salt()`` 只返回盐本身，调用方不得把盐塞进
   任何日志/fixture）。
"""
import hashlib
import hmac
import os
import re

REDACTED = "***REDACTED***"

# 字段名一旦匹配以下模式，整体替换为 REDACTED（不管值是什么形态）——这些是
# 授权凭据类字段，泄露即等价于账号被盗用，没有"保留关联性"的价值。
# 返修（P1-3）：不再用宽泛的裸 "session" 子串匹配一切——旧模式会把
# "session_id"（分析关联用的会话标识符，不是凭据）也当成凭据整体抹掉，
# 导致同一 session 的多条记录全部塌缩成同一个 "***REDACTED***"，分析管线
# 彻底失去关联能力。这里只保留真正的凭据字段（session_token/session_key/
# majiang_sid/cookie 等），标识符类字段改由 _IDENTIFIER_KEY_PATTERN 走
# 带盐稳定假 ID 路径（见 sanitize()）。
_SECRET_KEY_PATTERN = re.compile(
    r"(authorization|bearer|token|cookie|majiang_sid|password|passwd|secret|"
    r"portal_cookie|session_token|session_key|session_secret)",
    re.IGNORECASE,
)
# 分析关联用的标识符字段：不是凭据，泄露风险远低于 token/cookie，但直接
# 落盘/外发仍可能关联到真实用户。使用 stable_pseudo_id() 做带盐不可逆假名化，
# 同一 (domain, 原始值, 盐) 恒定映射到同一假 ID，保留跨记录关联能力；
# 没有配置盐时才退化为 REDACTED（安全默认：宁可丢关联，不可泄露原始值）。
_IDENTIFIER_KEY_PATTERN = re.compile(
    r"(session_id|user_id|nickname|player_id|account_id)",
    re.IGNORECASE,
)
# key -> stable_pseudo_id 的 domain 前缀映射，未命中时退化为小写字段名本身。
_IDENTIFIER_DOMAINS = {
    "session_id": "session",
    "user_id": "user",
    "nickname": "user",
    "player_id": "user",
    "account_id": "user",
}
# 历史令牌样式：64 位十六进制字符串（与 mj/security.py:TOKEN 保持一致口径）。
_HEX64_PATTERN = re.compile(r"\b[a-f0-9]{64}\b", re.IGNORECASE)
# 内嵌在任意字符串里的 "Bearer <token>" 片段（如 error 文本里复述了请求头）。
_BEARER_INLINE_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9\-_.=]{8,}", re.IGNORECASE)
_MAX_RECURSION_DEPTH = 64


class MissingSaltError(RuntimeError):
    """外发导出需要稳定假 ID 盐，但既没有显式传入也没有环境变量，且输入
    未被明确标记为 synthetic（测试固定盐场景应显式传参，不依赖本异常）。"""


def get_salt(explicit=None):
    """返回用于 HMAC 的盐。绝不把盐本身写进任何输出——调用方只应把返回值
    喂给 ``stable_pseudo_id()``，不应打印/落盘这个返回值。"""
    if explicit:
        return explicit
    return os.environ.get("MJ_SANITIZE_SALT") or None


def require_salt(explicit=None, *, allow_missing_if_synthetic=False):
    """外发场景的盐校验入口：缺盐且非 synthetic 时抛出 MissingSaltError。"""
    salt = get_salt(explicit)
    if salt:
        return salt
    if allow_missing_if_synthetic:
        return None
    raise MissingSaltError(
        "missing stable-ID salt: pass --salt / set MJ_SANITIZE_SALT env var, "
        "or explicitly mark the export as synthetic (--synthetic-source) if "
        "no real identifiers are involved"
    )


def stable_pseudo_id(domain, value, salt):
    """带域前缀的 HMAC-SHA256 稳定假 ID：``<domain>_<hexdigest[:24]>``。

    同一 (domain, value, salt) 组合恒定产出同一假 ID（跨事件关联所需），
    不可逆（HMAC 单向），且不同 domain 前缀相同 value 不会产生同一假 ID
    （HMAC 消息里已经拼接了 domain，不是单纯给同一哈希加前缀）。
    """
    if value is None:
        return None
    if not salt:
        raise MissingSaltError(f"missing salt while pseudonymizing domain={domain!r}")
    mac = hmac.new(salt.encode("utf-8"), f"{domain}:{value}".encode("utf-8"), hashlib.sha256)
    return f"{domain}_{mac.hexdigest()[:24]}"


def _looks_secret_key(key) -> bool:
    return bool(_SECRET_KEY_PATTERN.search(str(key)))


def _looks_identifier_key(key) -> bool:
    return bool(_IDENTIFIER_KEY_PATTERN.search(str(key)))


def _identifier_domain(key) -> str:
    lowered = str(key).lower()
    for name, domain in _IDENTIFIER_DOMAINS.items():
        if name in lowered:
            return domain
    return lowered


def _redact_string(text: str) -> str:
    text = _HEX64_PATTERN.sub(REDACTED, text)
    text = _BEARER_INLINE_PATTERN.sub(REDACTED, text)
    return text


def sanitize(value, _depth: int = 0, *, salt=None):
    """递归清洗 dict/list/tuple/str。

    - 字段名命中凭据模式（Authorization/token/cookie/password/...）的整体
      替换为 REDACTED——这类字段没有"保留关联性"的价值，泄露即等价于账号
      被盗用。
    - 字段名命中标识符模式（session_id/user_id/nickname/player_id/
      account_id）的：
      * 提供了 ``salt`` 时，替换为 ``stable_pseudo_id(domain, value, salt)``
        ——带盐不可逆假名化，保留同一原始值跨记录关联的能力（P1-3 返修：
        不能因为字段名里含 "session" 就直接整体丢弃，导致分析管线失去
        关联能力）；
      * 未提供 ``salt`` 时，退化为 REDACTED（安全默认：没有盐就不可能算出
        稳定假 ID，宁可丢失关联也不能泄露原始值）。
    - 普通字符串只替换检测到的令牌模式，不整体截断（截断由
      ``sanitize_text()`` 的 ``limit`` 参数单独负责）。
    """
    if _depth > _MAX_RECURSION_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _looks_secret_key(k):
                out[k] = REDACTED
            elif _looks_identifier_key(k):
                if salt and v is not None:
                    out[k] = stable_pseudo_id(_identifier_domain(k), v, salt)
                else:
                    out[k] = REDACTED
            else:
                out[k] = sanitize(v, _depth + 1, salt=salt)
        return out
    if isinstance(value, list):
        return [sanitize(v, _depth + 1, salt=salt) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize(v, _depth + 1, salt=salt) for v in value)
    if isinstance(value, str):
        return _redact_string(value)
    return value


def sanitize_text(text, limit=None) -> str:
    """字符串场景（errors.summary、CLI 打印文本等）：先脱敏再截断。"""
    cleaned = _redact_string(str(text)).strip().replace("\n", " ")
    if limit and len(cleaned) > limit:
        return cleaned[: limit] + "...(truncated)"
    return cleaned


def scan_for_leaks(value):
    """返回检测到的潜在泄露命中路径列表（供测试反例/CLI 自检使用），
    不修改 ``value``。命中条件与 ``sanitize()``/``_looks_secret_key`` 一致。"""
    hits = []

    def _walk(v, path):
        if isinstance(v, dict):
            for k, vv in v.items():
                if _looks_secret_key(k):
                    hits.append(f"{path}.{k} (secret-key)")
                _walk(vv, f"{path}.{k}")
        elif isinstance(v, (list, tuple)):
            for i, vv in enumerate(v):
                _walk(vv, f"{path}[{i}]")
        elif isinstance(v, str):
            if _HEX64_PATTERN.search(v):
                hits.append(f"{path} (hex64-token)")
            if _BEARER_INLINE_PATTERN.search(v):
                hits.append(f"{path} (bearer-token)")

    _walk(value, "$")
    return hits
