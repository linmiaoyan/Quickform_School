import json
import os
import tempfile
from dataclasses import dataclass


@dataclass
class QFLinkConfig:
    enabled: bool = True


def _config_path() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(base_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "qflink_config.json")


def load_qflink_config() -> QFLinkConfig:
    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        enabled = bool(raw.get("enabled", True))
        return QFLinkConfig(enabled=enabled)
    except FileNotFoundError:
        return QFLinkConfig()
    except Exception:
        # 配置文件损坏时兜底：默认启用，避免全站无法登录
        return QFLinkConfig()


def save_qflink_config(cfg: QFLinkConfig) -> None:
    path = _config_path()
    payload = {"enabled": bool(cfg.enabled)}
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".qflink_config.", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
