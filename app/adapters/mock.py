from __future__ import annotations

import logging

from .base import ChatAdapter

logger = logging.getLogger(__name__)


class MockChatAdapter(ChatAdapter):
    """本地模拟通道：把提醒写入应用日志，由网页端轮询展示。"""

    def send(self, recipient: str, content: str) -> bool:
        logger.info("[mock-chat] recipient=%s content=%s", recipient, content)
        return True
