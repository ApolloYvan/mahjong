# -*- coding: utf-8 -*-
"""令牌解析：显式参数 > MJ_TOKEN 环境变量 > 本地令牌文件。

2026-09-24：此前每个工具各自 os.environ['MJ_TOKEN']，于是每开一个新终端都要
重新 export，实盘里因此反复撞 401。令牌落一次盘，之后所有入口自动读。
文件权限设 0600、内容不打印，仅此而已——不做加密（测试赛令牌，随时可轮换）。
"""
import os
import stat

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOPED_FILE = os.path.join(_ROOT, ".mj_token")          # 参赛令牌（按赛事派发，仅显示一次）
GLOBAL_FILE = os.path.join(_ROOT, ".mj_token_global")   # 全局令牌（门户「我的 AI 身份」）
HOME_SCOPED = os.path.expanduser("~/.mahjong_token")

# 2026-09-24 实盘：两种令牌作用域互斥，混用直接被拒——
#   参赛令牌 调 /api/match        → 400 TOKEN_NOT_SCOPED
#   全局令牌 调 /me/rules、/me/ready → 400 TOKEN_NOT_SCOPED
# 所以不是"一个令牌配好就行"，而是按用途选。两个都存下来，程序自己挑。
_ORDER = {
    "scoped": ("MJ_TOKEN", SCOPED_FILE, HOME_SCOPED, "MJ_GLOBAL_TOKEN", GLOBAL_FILE),
    "global": ("MJ_GLOBAL_TOKEN", GLOBAL_FILE, "MJ_TOKEN", SCOPED_FILE, HOME_SCOPED),
}


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def resolve_token(cli_token=None, environ=None, kind="scoped"):
    """按用途取令牌：显式参数 > 该用途的环境变量/文件 > 另一种（兜底，可能被拒）。

    kind="scoped" 用于赛事（register/ready/me/rules），"global" 用于 /api/match。
    """
    environ = os.environ if environ is None else environ
    if cli_token:
        return cli_token
    # MJ_TOKEN_NO_FILE=1 关闭文件兜底：测试需要断言「无命令行参数、无环境变量 →
    # None」，不能依赖开发机上恰好存在 .mj_token。生产不设这个变量。
    no_file = str(environ.get("MJ_TOKEN_NO_FILE", "")).strip() not in ("", "0", "false")
    for item in _ORDER.get(kind, _ORDER["scoped"]):
        if os.path.sep in item:
            value = "" if no_file else _read(item)
        else:
            value = environ.get(item, "").strip()
        if value:
            return value
    return None


def save_token(token, kind="scoped"):
    """写入对应用途的令牌文件并收紧权限到 0600。返回写入路径。"""
    token = (token or "").strip()
    if not token:
        raise ValueError("空令牌")
    path = GLOBAL_FILE if kind == "global" else SCOPED_FILE
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def describe(kind="scoped"):
    """诊断用：令牌从哪来、多长——绝不返回令牌本身。"""
    for item in _ORDER.get(kind, _ORDER["scoped"]):
        if os.path.sep in item:
            value = _read(item)
            if value:
                return "%s（长度 %d）" % (item, len(value))
        elif os.environ.get(item, "").strip():
            return "%s 环境变量（长度 %d）" % (item, len(os.environ[item].strip()))
    return "未找到%s令牌" % ("全局" if kind == "global" else "参赛")
