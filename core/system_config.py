import json
import os
import tempfile
from dataclasses import dataclass, asdict
from typing import Any, Dict


@dataclass(frozen=True)
class SystemConfig:
    system_name: str = "QuickForm校园版演示"
    default_school: str = "温州科技高级中学"
    registration_enabled: bool = True
    registration_requires_approval: bool = False
    community_enabled: bool = False
    teams_enabled: bool = False


def _config_path() -> str:
    # Keep config out of git, colocate with uploads (already exists).
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "system_config.json")


def _approval_path() -> str:
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "pending_user_approvals.json")


def load_pending_usernames() -> set[str]:
    p = _approval_path()
    if not os.path.exists(p):
        return set()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or []
        if not isinstance(data, list):
            return set()
        out: set[str] = set()
        for it in data:
            s = (str(it) or "").strip()
            if s:
                out.add(s.lower())
        return out
    except Exception:
        return set()


def save_pending_usernames(pending: set[str]) -> None:
    p = _approval_path()
    arr = sorted({(str(x) or "").strip().lower() for x in (pending or set()) if (str(x) or "").strip()})
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="pending_users_", suffix=".json", dir=os.path.dirname(p))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, p)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _pending_users_path() -> str:
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "pending_users.json")


def load_pending_users() -> Dict[str, Any]:
    """Return a mapping of username_lower -> metadata dict."""
    p = _pending_users_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pending_users(data: Dict[str, Any]) -> None:
    p = _pending_users_path()
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


def load_system_config() -> SystemConfig:
    p = _config_path()
    if not os.path.exists(p):
        return SystemConfig()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return SystemConfig()
    except Exception:
        return SystemConfig()

    def _s(key: str, default: str) -> str:
        v = data.get(key)
        if v is None:
            return default
        return (str(v) or "").strip() or default

    def _b(key: str, default: bool) -> bool:
        v = data.get(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "yes", "y", "on"):
                return True
            if s in ("0", "false", "no", "n", "off"):
                return False
        return default

    return SystemConfig(
        system_name=_s("system_name", SystemConfig.system_name),
        default_school=(str(data.get("default_school") or "")).strip(),
        registration_enabled=_b("registration_enabled", SystemConfig.registration_enabled),
        registration_requires_approval=_b(
            "registration_requires_approval", SystemConfig.registration_requires_approval
        ),
        community_enabled=_b("community_enabled", SystemConfig.community_enabled),
        teams_enabled=_b("teams_enabled", SystemConfig.teams_enabled),
    )


def save_system_config(cfg: SystemConfig) -> None:
    p = _config_path()
    d: Dict[str, Any] = asdict(cfg)
    # Normalize
    d["system_name"] = (d.get("system_name") or "").strip() or SystemConfig.system_name
    d["default_school"] = (d.get("default_school") or "").strip()
    d["registration_enabled"] = bool(d.get("registration_enabled"))
    d["registration_requires_approval"] = bool(d.get("registration_requires_approval"))
    d["community_enabled"] = bool(d.get("community_enabled"))
    d["teams_enabled"] = bool(d.get("teams_enabled"))

    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="system_config_", suffix=".json", dir=os.path.dirname(p))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, p)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

