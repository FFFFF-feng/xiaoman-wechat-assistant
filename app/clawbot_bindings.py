"""Private allowlist. One backend process owns mutations; readers see atomic snapshots."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

from fastapi import HTTPException

lock = threading.RLock()
IDENTITY = re.compile(r"^[^\x00-\x20\x7f]{1,160}$")


def load_bindings() -> list[dict[str, str]]:
    try:
        path = Path(os.environ["CLAWBOT_BINDINGS_FILE"])
        if path.stat().st_size > 256 * 1024:
            raise ValueError()
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, list) or len(values) > 200:
            raise ValueError()
        pairs = set()
        for value in values:
            if not isinstance(value, dict) or set(value) != {"account_id", "sender_id"}:
                raise ValueError()
            pair = (value["account_id"], value["sender_id"])
            if not all(isinstance(v, str) and IDENTITY.fullmatch(v) for v in pair) or pair in pairs:
                raise ValueError()
            pairs.add(pair)
        return values  # An empty list means no users admitted, not a default user.
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "微信绑定尚未准备好") from None


def save_bindings(values: list[dict[str, str]]) -> None:
    path = Path(os.environ["CLAWBOT_BINDINGS_FILE"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".bindings-", suffix=".tmp", delete=False) as file:
            temporary = file.name
            json.dump(values, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
