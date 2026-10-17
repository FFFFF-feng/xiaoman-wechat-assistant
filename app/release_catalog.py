"""One checked-in catalog supplies both the public board and private notices."""
from datetime import date
import json
from pathlib import Path
import re
from pydantic import BaseModel, ConfigDict, Field

CATALOG = Path(__file__).resolve().parents[1] / "web/releases.json"


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=400)


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str = Field(pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=160)
    changes: list[Change] = Field(min_length=1, max_length=8)
    examples: list[str] = Field(min_length=1, max_length=5)
    note: str = Field(max_length=300)


def catalog():
    raw = CATALOG.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("Release catalog too large")
    values = json.loads(raw)
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("Invalid release catalog")
    releases = [Release.model_validate(value) for value in values]
    versions = [tuple(map(int, item.version.split('.'))) for item in releases]
    if len(set(versions)) != len(versions) or versions != sorted(versions, reverse=True):
        raise ValueError("Release versions must be unique, newest first")
    for item in releases:
        date.fromisoformat(item.date)
        for example in item.examples:
            if not isinstance(example, str) or not 1 <= len(example) <= 100 or re.search(r'[\x00-\x1f]', example):
                raise ValueError("Invalid release example")
    return releases


def board_url(base_url, version=None):
    anchor = 'release-' + version.replace('.', '-') if version else 'updates'
    return f"{base_url}/guide?view=updates#{anchor}"


def notice(item, base_url):
    points = '\n'.join('• ' + change.title for change in item.changes)
    return (f"小满更新啦 · v{item.version}\n{item.summary}\n\n{points}\n\n"
            f"试一句：{item.examples[0]}\n[看看这次的新功能]({board_url(base_url, item.version)})")
