from __future__ import annotations

import base64
import hashlib
import hmac
import os

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # pragma: no cover - 仅在依赖尚未安装时触发
    Cipher = algorithms = modes = None


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def wechat_dialogue_status() -> dict[str, object]:
    token = _env("WECHAT_DIALOGUE_TOKEN")
    aes_key = _env("WECHAT_DIALOGUE_AES_KEY")
    return {
        "enabled": _env_bool("WECHAT_DIALOGUE_ENABLED"),
        "signature_configured": bool(token),
        "encryption_configured": bool(aes_key),
        "encryption_required": _env_bool("WECHAT_DIALOGUE_REQUIRE_ENCRYPTION"),
        "crypto_available": Cipher is not None,
        "callback_path": "/api/channels/wechat-dialogue/task",
    }


def build_dialogue_signature(
    token: str,
    timestamp: str,
    skill_name: str,
    intent_name: str,
    query: str,
) -> str:
    """按微信对话开放平台文档顺序生成 MD5 签名。"""
    raw = f"{token}{timestamp}{skill_name}{intent_name}{query}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def verify_dialogue_signature(
    signature: str,
    timestamp: str,
    skill_name: str,
    intent_name: str,
    query: str,
) -> bool:
    token = _env("WECHAT_DIALOGUE_TOKEN")
    if not token or not signature:
        return False
    expected = build_dialogue_signature(token, timestamp, skill_name, intent_name, query)
    return hmac.compare_digest(expected.lower(), signature.lower())


def _aes_material() -> tuple[bytes, bytes]:
    if Cipher is None:
        raise RuntimeError("尚未安装 cryptography，无法处理 AES 加密消息")
    configured = _env("WECHAT_DIALOGUE_AES_KEY")
    if not configured:
        raise RuntimeError("尚未配置 WECHAT_DIALOGUE_AES_KEY")
    try:
        key = base64.b64decode(configured + "=" * (-len(configured) % 4), validate=True)
    except Exception as exc:
        raise ValueError("WECHAT_DIALOGUE_AES_KEY 不是有效的 Base64 密钥") from exc
    if len(key) != 32:
        raise ValueError("WECHAT_DIALOGUE_AES_KEY 解码后必须为 32 字节")
    return key, key[:16]


def encrypt_dialogue_text(plain_text: str) -> str:
    """使用平台约定的 AES-256-CBC 和 32 字节 PKCS#7 填充。"""
    key, iv = _aes_material()
    raw = plain_text.encode("utf-8")
    padding_size = 32 - len(raw) % 32
    padded = raw + bytes([padding_size]) * padding_size
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_dialogue_text(cipher_text: str) -> str:
    """解密平台消息；兼容 16 或 32 字节分组的 PKCS#7 填充。"""
    key, iv = _aes_material()
    try:
        encrypted = base64.b64decode(cipher_text.strip(), validate=True)
    except Exception as exc:
        raise ValueError("加密消息不是有效的 Base64") from exc
    if not encrypted or len(encrypted) % 16 != 0:
        raise ValueError("加密消息长度无效")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(encrypted) + decryptor.finalize()
    padding_size = padded[-1]
    if padding_size < 1 or padding_size > 32:
        raise ValueError("加密消息填充无效")
    if padded[-padding_size:] != bytes([padding_size]) * padding_size:
        raise ValueError("加密消息填充校验失败")
    try:
        return padded[:-padding_size].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("加密消息不是有效的 UTF-8 文本") from exc
