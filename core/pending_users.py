"""Registration approval (pending users) store.

校园版的“注册需管理员审核”不使用 Alembic，为了保持部署简单，使用 JSON 文件记录待审核用户名。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
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


def _entry_status(entry: Any) -> str:
    if isinstance(entry, dict):
        raw = entry.get('status')
        if raw is None or raw == '':
            return 'pending'
        try:
            st = str(raw).strip().lower()
        except Exception:
            return 'pending'
        return st if st in ('pending', 'rejected') else 'pending'
    return 'pending'


def set_user_pending(username: str, pending: bool, meta: dict[str, Any] | None = None) -> None:
    u = (username or "").strip()
    if not u:
        return
    with _LOCK:
        data = load_pending_users()
        if pending:
            row = dict(meta or {})
            row.setdefault('status', 'pending')
            data[u] = row
        else:
            data.pop(u, None)
        _save_pending_users(data)


def set_user_registration_rejected(username: str, *, reason: str = '', note: str = '', meta: dict[str, Any] | None = None) -> None:
    """标记注册审核拒绝（保留记录，登录时拦截）。"""
    u = (username or "").strip()
    if not u:
        return
    with _LOCK:
        data = load_pending_users()
        row = dict(data.get(u) or meta or {})
        if meta and isinstance(meta, dict):
            row.update(meta)
        row['status'] = 'rejected'
        row['reject_reason'] = (reason or note or '注册审核未通过').strip()
        row['rejected_at'] = datetime.now().isoformat(timespec='seconds')
        data[u] = row
        _save_pending_users(data)


def load_pending_registration_users() -> dict[str, Any]:
    """仅返回待审核（pending）用户，供管理后台列表展示。"""
    out: dict[str, Any] = {}
    for uname, meta in (load_pending_users() or {}).items():
        try:
            if _entry_status(meta) == 'pending':
                out[uname] = meta
        except Exception:
            continue
    return out


def is_user_pending(username: str) -> bool:
    u = (username or "").strip()
    if not u:
        return False
    with _LOCK:
        data = load_pending_users()
        entry = data.get(u)
        if entry is None:
            return False
        return _entry_status(entry) == 'pending'


def get_registration_reject_message(username: str) -> str | None:
    u = (username or "").strip()
    if not u:
        return None
    with _LOCK:
        entry = load_pending_users().get(u)
        if entry is None or _entry_status(entry) != 'rejected':
            return None
        if isinstance(entry, dict):
            return (entry.get('reject_reason') or entry.get('reject_note') or '注册审核未通过').strip()
        return '注册审核未通过'

