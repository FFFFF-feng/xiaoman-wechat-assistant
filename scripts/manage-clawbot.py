"""Loopback-only administration; display invitation URLs only with --show-link."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx


def main():
    parser = argparse.ArgumentParser(description="小满邀请制多人接入管理（仅本机）")
    parser.add_argument("action", choices=("invite", "members", "revoke"))
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--backend", default="http://127.0.0.1:18083")
    parser.add_argument("--label", default="体验用户")
    parser.add_argument("--public-url", help="邀请页 HTTPS 基础地址；本机浏览器测试可用 http://127.0.0.1:18083")
    parser.add_argument("--output", type=Path, help="新建的私有邀请 JSON 文件，不覆盖已有文件")
    parser.add_argument("--member", help="members 返回的不可读身份哈希，不是微信原始 ID")
    parser.add_argument("--show-link", action="store_true", help="仅签发邀请时在私有终端显示完整链接；请勿公开截图")
    args = parser.parse_args()
    if args.show_link and args.action != "invite":
        parser.error("--show-link 仅用于 invite")
    parsed = urlparse(args.backend)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path or parsed.query or parsed.fragment or parsed.username:
        parser.error("管理接口必须是本机 http://127.0.0.1:端口")
    token = args.token_file.read_text(encoding="utf-8").strip()
    if len(token) != 64 or any(v not in "0123456789abcdef" for v in token):
        parser.error("桥接密钥文件无效")
    with httpx.Client(base_url=args.backend, headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=10) as client:
        prefix = "/api/internal/clawbot/enrollment"
        if args.action == "invite":
            url = urlparse(args.public_url or "")
            if (not args.output or not url.hostname or url.username or url.password or url.query or url.fragment
                    or url.path not in ("", "/") or not (url.scheme == "https" or url.scheme == "http" and url.hostname == "127.0.0.1")):
                parser.error("指定 --output 私有文件与 --public-url HTTPS 地址")
            if args.output.exists(): parser.error("输出文件已存在，不覆盖旧邀请")
            response = client.post(prefix + "/invites", json={"label": args.label})
            if response.status_code != 200: raise RuntimeError(f"签发失败 HTTP {response.status_code}（检查私有配置）")
            result = response.json()
            result["url"] = args.public_url.rstrip("/") + "/join#invite=" + result.pop("invite")
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(result, output, ensure_ascii=False, indent=2)
            print("邀请已保存到指定私有文件，有效期 30 分钟；只发给该体验者。")
            if args.show_link:
                print("专属邀请链接（含短时凭证，请勿公开）：")
                print(result["url"])
                print("每位体验者重新生成一次；不要给已经绑定的微信重复扫码。")
        elif args.action == "members":
            response = client.get(prefix + "/members")
            if response.status_code != 200: raise RuntimeError(f"读取失败 HTTP {response.status_code}")
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        else:
            if not args.member or not args.member.startswith("clawbot:"): parser.error("指定 --member 成员哈希")
            response = client.post(prefix + f"/members/{args.member}/revoke")
            if response.status_code != 200: raise RuntimeError(f"撤销失败 HTTP {response.status_code}")
            print("已撤销接入；旧日程链接失效，历史任务和记录保留。")


if __name__ == "__main__":
    main()
