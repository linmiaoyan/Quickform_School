import json
import os
import tempfile
from typing import Dict, Any


def _pending_path() -> str:
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "pending_users.json")


def load_pending_users() -> Dict[str, Any]:
    p = _pending_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pending_users(data: Dict[str, Any]) -> None:
    p = _pending_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="pending_users_", suffix=".json", dir=os.path.dirname(p))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data or {}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, p)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def is_user_pending(username: str) -> bool:
    u = (username or "").strip().lower()
    if not u:
        return False
    data = load_pending_users()
    rec = data.get(u)
    if rec is None:
        return False
    if isinstance(rec, dict):
        return bool(rec.get("pending", True))
    return True


def set_user_pending(username: str, pending: bool, meta: Dict[str, Any] | None = None) -> None:
    u = (username or "").strip().lower()
    if not u:
        return
    data = load_pending_users()
    if pending:
        rec = {"pending": True}
        if meta and isinstance(meta, dict):
            rec.update(meta)
        data[u] = rec
    else:
        # remove record to keep file small
        data.pop(u, None)
    save_pending_users(data)

