"""检查拟发布的Git文件，不输出真实凭据；不能代替人工安全审计。"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = {".env.example", ".env.official.example"}
PRIVATE_DIRS = {".venv", ".cache", "data", "logs", "output", "backups", "__pycache__"}
PRIVATE_DOCS = {
    "docs/本地运行与测试.md", "docs/正式号本机联调.md",
    "docs/正式公众号接入.md", "docs/审核等待期与新服务号接入计划.md",
}
PRIVATE_SUFFIXES = {".pem", ".key", ".db", ".sqlite", ".sqlite3", ".log", ".zip", ".pyc"}
CREDENTIAL = re.compile(r"(?:KEY|SECRET|TOKEN|PASSWORD)")
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "api-key-format": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], stderr=subprocess.PIPE)


def assignments(text: str):
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            name, value = match.groups()
            yield name, value.strip().strip("\"'")


def local_credentials() -> set[str]:
    values: set[str] = set()
    for path in ROOT.glob(".env*"):
        if not path.is_file() or path.name in EXAMPLES:
            continue
        text = path.read_text(encoding="utf-8-sig")
        for name, value in assignments(text):
            if CREDENTIAL.search(name) and len(value) >= 8:
                values.add(value)
    return values


def path_problems(name: str) -> list[str]:
    path = Path(name)
    problems = []
    if (path.name == ".env" or path.name.startswith(".env.")) and name not in EXAMPLES:
        problems.append("private-env")
    if any(part in PRIVATE_DIRS for part in path.parts):
        problems.append("private-data-directory")
    if path.suffix.lower() in PRIVATE_SUFFIXES or path.name.endswith((".db-wal", ".db-shm")):
        problems.append("private-or-generated-file")
    if name in PRIVATE_DOCS:
        problems.append("internal-handover-document")
    return problems


def content_problems(name: str, data: bytes, credentials: set[str]) -> list[str]:
    if len(data) > 2 * 1024 * 1024:
        return ["large-file-needs-manual-review"]
    if b"\x00" in data:
        return ["binary-file-needs-manual-review"]
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ["non-utf8-file-needs-manual-review"]
    problems = [label for label, pattern in PATTERNS.items() if pattern.search(text)]
    if any(value in text for value in credentials):
        problems.append("matches-local-private-credential")
    if name in EXAMPLES:
        if any(CREDENTIAL.search(key) and value for key, value in assignments(text)):
            problems.append("nonempty-example-credential")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="检查暂存内容而非工作区")
    args = parser.parse_args()
    try:
        git("rev-parse", "--show-toplevel")
        names_bytes = (git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
                       if args.staged else git("ls-files", "-z"))
        names = [item.decode("utf-8") for item in names_bytes.split(b"\0") if item]
        if not names:
            print("FAIL: 没有拟检查的Git文件，请先核对并暂存上传范围。")
            return 2
        credentials = local_credentials()
        issues = []
        for name in names:
            problems = path_problems(name)
            if not problems:
                data = git("show", f":{name}") if args.staged else (ROOT / name).read_bytes()
                problems.extend(content_problems(name, data, credentials))
            issues.extend((name, problem) for problem in problems)
        if issues:
            for name, problem in issues:
                print(f"FAIL: {name} [{problem}]")
            print("检查未通过；没有输出任何匹配到的凭据值。")
            return 1
        print(f"PASS: {len(names)}个Git文件通过路径、模板、常见秘密格式及本机凭据比对。")
        print("仍需人工确认业务数据、代码权属与可见性；不保证发现全部秘密。")
        return 0
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        print("FAIL: 无法完整读取Git文件或私有配置；未输出内容，请人工核查环境。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
