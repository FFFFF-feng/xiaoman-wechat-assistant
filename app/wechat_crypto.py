"""微信公众号 AES-256-CBC 消息协议；不与对话平台的加密协议混用。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from xml.etree.ElementTree import Element, SubElement, tostring

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WechatCryptoError(ValueError):
    pass


def encryption_mode() -> str:
    mode = os.getenv("WECHAT_ENCRYPTION_MODE", "plain").strip().lower()
    if mode not in {"plain", "compatible", "safe"}:
        raise WechatCryptoError("WECHAT_ENCRYPTION_MODE 必须是 plain、compatible 或 safe")
    return mode


class WechatCrypto:
    MAX_MESSAGE_BYTES = 128 * 1024

    def __init__(self, token: str, encoding_aes_key: str, app_id: str):
        if not token or not app_id:
            raise WechatCryptoError("加密模式需要 WECHAT_TOKEN 和 WECHAT_APP_ID")
        if not re.fullmatch(r"[A-Za-z0-9+/]{43}", encoding_aes_key):
            raise WechatCryptoError("WECHAT_ENCODING_AES_KEY 必须是平台生成的 43 位密钥")
        self.key = base64.b64decode(encoding_aes_key + "=", validate=True)
        if len(self.key) != 32:
            raise WechatCryptoError("EncodingAESKey 长度不正确")
        self.token = token
        self.app_id = app_id.encode("utf-8")

    @classmethod
    def from_env(cls) -> "WechatCrypto":
        return cls(*(os.getenv(name, "").strip() for name in
                     ("WECHAT_TOKEN", "WECHAT_ENCODING_AES_KEY", "WECHAT_APP_ID")))

    def signature(self, ciphertext: str, timestamp: str, nonce: str) -> str:
        return hashlib.sha1("".join(sorted((self.token, timestamp, nonce, ciphertext))).encode()).hexdigest()

    def verify(self, signature: str, ciphertext: str, timestamp: str, nonce: str) -> bool:
        return bool(signature and timestamp and nonce and hmac.compare_digest(
            self.signature(ciphertext, timestamp, nonce), signature))

    def decrypt(self, ciphertext: str) -> bytes:
        # 调用者必须先验签；这里仍对填充、长度和接收方逐项验证。
        try:
            encrypted = base64.b64decode(ciphertext, validate=True)
            if not encrypted or len(encrypted) % 16 or len(encrypted) > self.MAX_MESSAGE_BYTES + 1024:
                raise ValueError
            decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
            padded = decryptor.update(encrypted) + decryptor.finalize()
            padding = padded[-1]
            if not 1 <= padding <= 32 or padded[-padding:] != bytes([padding]) * padding:
                raise ValueError
            plain = padded[:-padding]
            if len(plain) < 20:
                raise ValueError
            length = struct.unpack("!I", plain[16:20])[0]
            if length > self.MAX_MESSAGE_BYTES or 20 + length >= len(plain):
                raise ValueError
            if not hmac.compare_digest(plain[20 + length:], self.app_id):
                raise ValueError
            return plain[20:20 + length]
        except (ValueError, TypeError, IndexError, struct.error) as exc:
            raise WechatCryptoError("微信加密消息无效或 AppID 不匹配") from exc

    def encrypt(self, message: bytes) -> str:
        if len(message) > self.MAX_MESSAGE_BYTES:
            raise WechatCryptoError("微信消息体过大")
        plain = secrets.token_bytes(16) + struct.pack("!I", len(message)) + message + self.app_id
        padding = 32 - len(plain) % 32
        plain += bytes([padding]) * padding
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).encryptor()
        return base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode("ascii")

    def reply(self, message: bytes, nonce: str, timestamp: str | None = None) -> bytes:
        timestamp = timestamp or str(int(time.time()))
        nonce = nonce or secrets.token_hex(8)
        ciphertext = self.encrypt(message)
        root = Element("xml")
        for name, value in (("Encrypt", ciphertext), ("MsgSignature", self.signature(ciphertext, timestamp, nonce)),
                            ("TimeStamp", timestamp), ("Nonce", nonce)):
            SubElement(root, name).text = value
        return tostring(root, encoding="utf-8")
