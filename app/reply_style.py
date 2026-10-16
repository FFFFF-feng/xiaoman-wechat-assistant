"""Shared, versioned writing policy; never rewrite task parameters or receipts."""
from pathlib import Path


NATURAL_CHAT_POLICY = (
    Path(__file__).resolve().parents[1]
    / "skills/xiaoman-natural-chat/references/chat-policy.txt"
).read_text(encoding="utf-8").strip()
