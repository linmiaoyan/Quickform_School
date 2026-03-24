"""AI 密钥加解密工具（Fernet）。"""
import base64
import hashlib
import os
from typing import Iterable

from cryptography.fernet import Fernet, InvalidToken

ENC_PREFIX = "enc:v1:"
AI_SECRET_FIELDS = (
    "deepseek_api_key",
    "doubao_api_key",
    "doubao_secret_key",
    "qwen_api_key",
    "chat_server_api_token",
    "moonshot_api_key",
    "glm_api_key",
    "ernie_api_key",
    "ernie_secret_key",
    "openrouter_api_key",
)


def _build_fernet_key() -> bytes:
    """优先使用 AI_CONFIG_ENCRYPTION_KEY；否则基于 SECRET_KEY 派生。"""
    raw = (os.getenv("AI_CONFIG_ENCRYPTION_KEY") or "").strip()
    if raw:
        try:
            # 已经是 Fernet key（urlsafe_b64 32-byte）
            decoded = base64.urlsafe_b64decode(raw.encode("utf-8"))
            if len(decoded) == 32:
                return raw.encode("utf-8")
        except Exception:
            # 当成普通字符串继续派生
            pass
        return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())

    secret = (os.getenv("SECRET_KEY") or "").strip()
    if not secret or secret == "your_secret_key_here":
        raise RuntimeError("缺少有效加密主密钥：请配置 SECRET_KEY 或 AI_CONFIG_ENCRYPTION_KEY")
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


def _fernet() -> Fernet:
    return Fernet(_build_fernet_key())


def encrypt_text(plain_text: str) -> str:
    value = (plain_text or "").strip()
    if not value:
        return ""
    if value.startswith(ENC_PREFIX):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return f"{ENC_PREFIX}{token}"


def decrypt_text(cipher_text: str) -> str:
    value = (cipher_text or "").strip()
    if not value:
        return ""
    if not value.startswith(ENC_PREFIX):
        # 兼容历史明文
        return value
    token = value[len(ENC_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError("AI 密钥解密失败：请检查 SECRET_KEY / AI_CONFIG_ENCRYPTION_KEY 是否与历史一致")


def decrypt_ai_config_inplace(ai_config, fields: Iterable[str] = AI_SECRET_FIELDS):
    if not ai_config:
        return ai_config
    for field in fields:
        setattr(ai_config, field, decrypt_text(getattr(ai_config, field, "")))
    return ai_config


def encrypt_ai_config_inplace(ai_config, fields: Iterable[str] = AI_SECRET_FIELDS):
    if not ai_config:
        return ai_config
    for field in fields:
        setattr(ai_config, field, encrypt_text(getattr(ai_config, field, "")))
    return ai_config
