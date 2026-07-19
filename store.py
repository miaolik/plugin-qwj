"""群文件插件共享存储：Cookie / 登录令牌 / 配置。

本文件不含指令处理器，仅作为被 清空群文件.py 与 web_login.py 复用的工具模块。
"""
import json
import os
import re
import secrets
import time
from datetime import datetime

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(PLUGIN_DIR, "json")
USERS_DIR = os.path.join(STATE_DIR, "users")  # 每个用户一个目录：users/<用户ID>/{ck.json,log.txt}
CK_DIR = os.path.join(STATE_DIR, "ck")  # 旧版每用户单文件，自动迁移
COOKIE_FILE = os.path.join(STATE_DIR, "pancookie.json")  # 更旧版单文件，自动迁移
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")
TOKEN_FILE = os.path.join(STATE_DIR, "tokens.json")
LOG_FILE = os.path.join(PLUGIN_DIR, "mm.txt")

COOKIE_TTL = 31 * 24 * 3600
TOKEN_TTL = 15 * 60


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, data: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# ---------- 用户目录（每个用户一个文件夹：json/users/<用户ID>/）----------
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_id(user_id) -> str:
    return _SAFE_ID_RE.sub("_", str(user_id))


def user_dir(user_id) -> str:
    return os.path.join(USERS_DIR, _safe_id(user_id))


def ensure_user_dir(user_id) -> str:
    path = user_dir(user_id)
    os.makedirs(path, exist_ok=True)
    return path


def _ck_file(user_id: str) -> str:
    return os.path.join(user_dir(user_id), "ck.json")


def _user_log_file(user_id: str) -> str:
    return os.path.join(user_dir(user_id), "log.txt")


def log_user(user_id, msg: str):
    """写入该用户目录下的 log.txt，同时并入全局日志。"""
    log(msg)
    try:
        ensure_user_dir(user_id)
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        with open(_user_log_file(user_id), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _migrate_legacy_cookies():
    """把旧存储迁移到 users/<ID>/ck.json：
    1) json/ck/<ID>.json（上一版每用户单文件）
    2) json/pancookie.json（更旧的单文件字典）
    迁移后把来源改名，避免重复迁移。
    """
    # 1) ck/<ID>.json
    if os.path.isdir(CK_DIR):
        for name in os.listdir(CK_DIR):
            if not name.endswith(".json"):
                continue
            info = _read_json(os.path.join(CK_DIR, name))
            uid = info.get("user_id") or name[:-5]
            if info.get("cookie") and not os.path.exists(_ck_file(uid)):
                ensure_user_dir(uid)
                with open(_ck_file(uid), "w", encoding="utf-8") as f:
                    json.dump({"user_id": str(uid), "cookie": info["cookie"],
                               "expire": info.get("expire", 0)}, f)
        try:
            os.replace(CK_DIR, CK_DIR + ".migrated")
        except Exception:
            pass
    # 2) pancookie.json
    if os.path.exists(COOKIE_FILE):
        data = _read_json(COOKIE_FILE)
        for uid, info in data.items():
            if isinstance(info, dict) and info.get("cookie") and not os.path.exists(_ck_file(uid)):
                ensure_user_dir(uid)
                with open(_ck_file(uid), "w", encoding="utf-8") as f:
                    json.dump({"user_id": str(uid), "cookie": info["cookie"],
                               "expire": info.get("expire", 0)}, f)
        try:
            os.replace(COOKIE_FILE, COOKIE_FILE + ".migrated")
        except Exception:
            pass


def get_user_cookie(user_id: str):
    _migrate_legacy_cookies()
    info = _read_json(_ck_file(user_id))
    if not info or not info.get("cookie"):
        return None
    if info.get("expire", 0) <= time.time():
        try:
            os.remove(_ck_file(user_id))
        except Exception:
            pass
        return None
    return info["cookie"]


def set_user_cookie(user_id: str, cookie_str: str):
    ensure_user_dir(user_id)
    with open(_ck_file(user_id), "w", encoding="utf-8") as f:
        json.dump({"user_id": str(user_id), "cookie": cookie_str,
                   "expire": time.time() + COOKIE_TTL}, f)


def get_skey(cookie_str: str):
    m = re.search(r"(?:^|;\s*)skey=([^;]+)", cookie_str or "")
    return m.group(1) if m else None


def calc_bkn(skey: str) -> int:
    """QQ 通用 g_tk：hash33，初值 5381。"""
    h = 5381
    for c in skey:
        h += (h << 5) + ord(c)
    return h & 0x7fffffff


# ---------- 管理员白名单 ----------
# 预置管理员（首次运行自动写入 config.json，可用指令增删）
DEFAULT_ADMINS = ["538389445D765D2988BFE31506C54799"]


def get_admins() -> list:
    data = _read_json(CONFIG_FILE)
    if "admins" not in data:
        data["admins"] = list(DEFAULT_ADMINS)
        _write_json(CONFIG_FILE, data)
    admins = data.get("admins") or []
    return [str(a) for a in admins]


def is_admin(user_id) -> bool:
    return str(user_id) in get_admins()


def ensure_admin_dirs():
    """为所有管理员（含预置）建好各自的用户目录。"""
    for uid in get_admins():
        ensure_user_dir(uid)


def add_admin(user_id) -> bool:
    """返回 True 表示新增成功，False 表示已存在。新增时自动建好其用户目录。"""
    admins = get_admins()
    if str(user_id) in admins:
        return False
    admins.append(str(user_id))
    data = _read_json(CONFIG_FILE)
    data["admins"] = admins
    _write_json(CONFIG_FILE, data)
    ensure_user_dir(user_id)
    return True


def remove_admin(user_id) -> bool:
    """返回 True 表示删除成功，False 表示原本不在名单。"""
    admins = get_admins()
    if str(user_id) not in admins:
        return False
    admins = [a for a in admins if a != str(user_id)]
    data = _read_json(CONFIG_FILE)
    data["admins"] = admins
    _write_json(CONFIG_FILE, data)
    return True


# ---------- 配置 ----------
def get_base_url() -> str:
    return (_read_json(CONFIG_FILE).get("base_url") or "").rstrip("/")


def set_base_url(url: str):
    data = _read_json(CONFIG_FILE)
    data["base_url"] = url.rstrip("/")
    _write_json(CONFIG_FILE, data)


# ---------- 登录令牌 ----------
def _load_tokens() -> dict:
    data = _read_json(TOKEN_FILE)
    now = time.time()
    valid = {t: i for t, i in data.items()
             if isinstance(i, dict) and i.get("expire", 0) > now}
    if len(valid) != len(data):
        _write_json(TOKEN_FILE, valid)
    return valid


def create_login_token(user_id: str) -> str:
    tokens = _load_tokens()
    token = secrets.token_urlsafe(24)
    tokens[token] = {"uid": str(user_id), "expire": time.time() + TOKEN_TTL}
    _write_json(TOKEN_FILE, tokens)
    return token


def resolve_login_token(token: str):
    """令牌 → user_id（有效期内），无效返回 None。不消费，允许多次轮询。"""
    if not token:
        return None
    info = _load_tokens().get(token)
    return info.get("uid") if info else None
