"""上线前静态检查：禁止代码与 Git 跟踪文件出现令牌样式。"""
import glob
import hashlib
import os
import re

TOKEN = re.compile(r"\b[a-f0-9]{64}\b", re.IGNORECASE)


def scan(root="."):
    hits = []
    for path in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        if not os.path.isfile(path) or ".git" in path or "models" in path:
            continue
        try:
            with open(path, encoding="utf-8") as source:
                text = source.read()
        except (UnicodeDecodeError, OSError):
            continue
        if TOKEN.search(text):
            hits.append(path)
    return hits


def token_fingerprint(token):
    """返回令牌的安全指纹：SHA-256 摘要的前 8 位十六进制字符，用于日志/CLI
    输出里区分"这是哪个令牌"而不泄露原值（8 位远短于 64 位令牌样式，且
    单向哈希不可逆）。``token`` 为空/None 时返回 None。"""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]

