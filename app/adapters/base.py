from __future__ import annotations

from abc import ABC, abstractmethod


class ChatAdapter(ABC):
    """可替换的消息通道接口，不绑定任何真实微信 Hook。"""

    @abstractmethod
    def send(self, recipient: str, content: str) -> bool:
        raise NotImplementedError
