"""Prepare a NEW private runtime, then activate the installed official bridge.

Does not install packages, log in, start processes or touch an existing deployment.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parent.parent
MARKER = "xiaoman-server-runtime-v1\n"


def write_private(path, content, *, replace=False):
    if path.is_symlink() or (path.exists() and not replace):
        raise ValueError("目标文件已存在或为符号链接，不覆盖")
    if replace:
        descriptor, temporary = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def init_runtime(root, public_url):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("使用新的绝对路径，不使用符号链接")
    root = root.resolve()
    if root == PROJECT or root in PROJECT.parents:
        raise ValueError("运行目录不能是源码目录或它的上级目录")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("运行目录非空；迁移时不能重新初始化，先阅读部署文档")
    url = urlparse(public_url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password or
            url.query or url.fragment or url.path not in ("", "/") or url.port not in (None, 443)):
        raise ValueError("日程地址必须是无凭据、无路径的 HTTPS 域名，使用443端口")
    for directory in (root, root / "state", root / "state/xiaoman", root / "home",
                      root / "workspace", root / "npm", root / "logs"):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = root / "state/xiaoman"
    bridge_key, gateway_key = secrets.token_hex(32), secrets.token_hex(32)
    write_private(private / ".xiaoman-bridge", MARKER)
    write_private(private / "bridge.key", bridge_key + "\n")
    write_private(private / "bindings.json", "[]\n")
    write_private(private / "deployment.json", json_text({"project": str(PROJECT), "activated": False}))
    # This is OpenClaw's local identity, NOT the WeChat contact's profile.
    write_private(root / "workspace/avatar.svg", (PROJECT / "web/paw.svg").read_text(encoding="utf-8"))
    write_private(root / "workspace/IDENTITY.md", "# IDENTITY\n\n- Name: 小满\n- Emoji: 🐾\n- Avatar: avatar.svg\n\n此身份仅用于本地后台，不会修改微信联系人资料。\n")
    config = {
        "gateway": {"mode": "local", "bind": "loopback", "port": 18793,
                    "auth": {"mode": "token", "token": gateway_key}, "controlUi": {"enabled": False}},
        "session": {"dmScope": "per-account-channel-peer"},
        "tools": {"profile": "minimal", "deny": ["*"]}, "browser": {"enabled": False},
        "cron": {"enabled": False, "triggers": {"enabled": False}}, "hooks": {"enabled": False},
        "logging": {"level": "info", "file": str(root / "logs/openclaw.log")},
        "agents": {"defaults": {"workspace": str(root / "workspace"), "heartbeat": {"every": "0m"},
                    "model": {"primary": "xiaoman/bridge-required", "fallbacks": []},
                    "models": {"xiaoman/bridge-required": {}}, "thinkingDefault": "off", "timeoutSeconds": 90},
                   "entries": {"main": {
                               "identity": {"name": "小满", "emoji": "🐾", "avatar": "avatar.svg"}}}},
        "models": {"providers": {"xiaoman": {
            "baseUrl": "http://127.0.0.1:18083/bridge-only", "api": "openai-completions",
            "apiKey": "not-a-model-endpoint", "models": [{"id": "bridge-required", "name": "Xiaoman bridge only",
                "input": ["text"], "contextWindow": 32768, "maxTokens": 512, "reasoning": False,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}},
        "plugins": {"slots": {"memory": "none"}},
    }
    write_private(root / "state/openclaw.json", json_text(config))
    values = {
        "APP_ENV": "clawbot", "TIMEZONE": "Asia/Shanghai", "TZ": "Asia/Shanghai",
        "DATABASE_URL": "sqlite:///" + (private / "tasks.db").as_posix(), "REMINDER_CHECK_SECONDS": "5",
        "MODEL_ENABLED": "false", "MODEL_NAME": "qwen3.8-flash",
        "MODEL_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1", "MODEL_API_KEY": "",
        "DASHSCOPE_API_KEY": "", "ALIYUN_API_KEY": "", "CHAT_HISTORY_LIMIT": "10",
        "CLAWBOT_ENABLED": "true", "CLAWBOT_BRIDGE_TOKEN": bridge_key,
        "CLAWBOT_BINDINGS_FILE": (private / "bindings.json").as_posix(),
        "CLAWBOT_REMINDERS_ENABLED": "true", "CLAWBOT_REMINDER_MAX_LATENESS_SECONDS": "300",
        "CLAWBOT_ENROLLMENT_ENABLED": "true", "CLAWBOT_MAX_USERS": "10",
        "PUBLIC_BASE_URL": public_url.rstrip("/"), "CLAWBOT_PUBLIC_URL_FILE": "",
        "WECHAT_ENABLED": "false", "WECHAT_DIALOGUE_ENABLED": "false",
        "WECHAT_APP_ID": "", "WECHAT_APP_SECRET": "", "WECHAT_TOKEN": "", "WECHAT_ENCODING_AES_KEY": "",
    }
    write_private(private / "runtime.env", "# Private generated configuration; do not publish.\n" +
                  "".join(f"{name}={json.dumps(value, ensure_ascii=False)}\n" for name, value in values.items()))
    return root


def activate_runtime(root):
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("运行目录必须是已初始化的绝对路径")
    private = root / "state/xiaoman"
    if (private / ".xiaoman-bridge").read_text(encoding="utf-8") != MARKER:
        raise ValueError("不是本脚本初始化的运行目录，不改配置")
    metadata = json.loads((private / "deployment.json").read_text(encoding="utf-8"))
    if metadata.get("project") != str(PROJECT):
        raise ValueError("源码位置已改变，不能猜测新插件路径")
    config_path = root / "state/openclaw.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if (config.get("gateway", {}).get("bind") != "loopback" or
            config.get("gateway", {}).get("auth", {}).get("mode") != "token" or
            config.get("session", {}).get("dmScope") != "per-account-channel-peer"):
        raise ValueError("安全基线已改变，不改配置")
    if metadata.get("activated"):
        return False  # Never rewrite a running deployment on a second invocation.
    package = json.loads((root / "npm/node_modules/openclaw/package.json").read_text(encoding="utf-8"))
    # 2026.10.1 stores official installation inventory outside openclaw.json.
    # Inspect only package manifests under this new, owned runtime; do not read or edit its inventory DB.
    legacy = config.get("plugins", {}).get("installs", {}).get("openclaw-weixin", {}).get("installPath")
    candidates = list((root / "state/npm/projects").glob(
        "*/node_modules/@tencent-weixin/openclaw-weixin/package.json"))
    candidates.append(root / "state/extensions/openclaw-weixin/package.json")
    if legacy:
        candidates.append(Path(legacy) / "package.json")
    installed = {item.resolve() for item in candidates if item.is_file() and
                 item.resolve().is_relative_to(root.resolve())}
    if package.get("version") != "2026.10.1" or len(installed) != 1:
        raise ValueError("先安装 OpenClaw 2026.10.1 和官方微信插件")
    native = json.loads(next(iter(installed)).read_text(encoding="utf-8"))
    if native.get("name") != "@tencent-weixin/openclaw-weixin" or native.get("version") != "2.4.10":
        raise ValueError("官方微信插件版本不是已验收的2.4.10")
    plugins = config.setdefault("plugins", {})
    plugins.update({"allow": ["openclaw-weixin", "xiaoman-bridge"], "slots": {"memory": "none"},
                    "load": {"paths": [str(PROJECT / "integrations/openclaw-xiaoman")]},
                    "entries": {"openclaw-weixin": {"enabled": True}, "xiaoman-bridge": {
                        "enabled": True, "hooks": {"allowConversationAccess": True}, "config": {
                            "backendUrl": "http://127.0.0.1:18083", "tokenFile": str(private / "bridge.key"),
                            "bindingsFile": str(private / "bindings.json")}}}})
    config["channels"] = {"openclaw-weixin": {"enabled": True, "quoteCache": {"enabled": False}}}
    write_private(config_path, json_text(config), replace=True)
    metadata["activated"] = True
    write_private(private / "deployment.json", json_text(metadata), replace=True)
    return True


def main():
    parser = argparse.ArgumentParser(description="准备独立小满服务，不启动或修改已有服务")
    parser.add_argument("action", choices=("init", "activate"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--public-url")
    args = parser.parse_args()
    try:
        if args.action == "init":
            if not args.public_url:
                parser.error("init 需要 --public-url 实际固定HTTPS域名")
            init_runtime(args.root, args.public_url)
            print("已初始化新的私有目录；密钥未输出。模型默认关闭，请在 runtime.env 私下配置。")
        else:
            changed = activate_runtime(args.root)
            print("已启用小满连接器；未扫码、未启动网关。" if changed else "此前已启用，未覆盖配置或绑定。")
    except (ValueError, OSError, json.JSONDecodeError) as error:
        # File paths are fine; neither exception messages nor configuration contents are printed.
        print(f"准备失败（{type(error).__name__}）；未输出凭据。检查目录、版本和部署文档。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
