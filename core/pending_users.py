"""Registration approval (pending users) store.

校园版的“注册需管理员审核”不使用 Alembic，为了保持部署简单，使用 JSON 文件记录待审核用户名。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any


_LOCK = threading.Lock()


def _pending_users_path() -> str:
    # Prefer explicit env; otherwise store under uploads/ which is writable in common deploys.
    base = os.getenv("PENDING_USERS_JSON_PATH")
    if base and base.strip():
        return base.strip()
    # Fallback: repo root uploads/pending_users.json
    here = os.path.abspath(os.path.dirname(__file__))
    root = os.path.abspath(os.path.join(here, ".."))
    uploads = os.path.join(root, "uploads")
    os.makedirs(uploads, exist_ok=True)
    return os.path.join(uploads, "pending_users.json")


def load_pending_users() -> dict[str, Any]:
    path = _pending_users_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_pending_users(data: dict[str, Any]) -> None:
    path = _pending_users_path()
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def set_user_pending(username: str, pending: bool, meta: dict[str, Any] | None = None) -> None:
    u = (username or "").strip()
    if not u:
        return
    with _LOCK:
        data = load_pending_users()
        if pending:
            data[u] = meta or {}
        else:
            data.pop(u, None)
        _save_pending_users(data)


def is_user_pending(username: str) -> bool:
    u = (username or "").strip()
    if not u:
        return False
    with _LOCK:
        data = load_pending_users()
        return u in data

