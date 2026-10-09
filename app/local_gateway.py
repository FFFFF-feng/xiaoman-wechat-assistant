"""本机微信联调的路径白名单网关；公网不暴露调试/模型/本地任务接口。"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CALLBACK = "/api/channels/wechat/callback"
READ_ONLY_PATHS = {"/schedule", "/static/schedule.css", "/static/schedule.js", "/static/paw.svg"}
SCHEDULE_ACTION = re.compile(r"/api/schedule/tasks/[1-9][0-9]*/(?:complete|snooze|cancel)")


def allowed_path(path: str, method: str) -> bool:
    if path == CALLBACK:
        return method in {"GET", "POST"}
    if path in READ_ONLY_PATHS or path == "/api/schedule/tasks":
        return method in {"GET", "HEAD"}
    return method == "POST" and bool(SCHEDULE_ACTION.fullmatch(path))


def create_gateway(upstream_port: int = 18082, *, transport=None) -> FastAPI:
    if not 1024 <= upstream_port <= 65535:
        raise ValueError("本地上游端口无效")
    upstream = f"http://127.0.0.1:{upstream_port}"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, transport=transport,
            timeout=httpx.Timeout(25, connect=3, pool=3),
        ) as client:
            app.state.proxy_client = client
            yield

    gateway = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @gateway.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(request: Request, path: str):
        if not allowed_path(request.url.path, request.method):
            return Response(status_code=404)
        length = request.headers.get("content-length")
        try:
            if length and (int(length) < 0 or int(length) > MAX_REQUEST_BYTES):
                return Response(status_code=413)
        except ValueError:
            return Response(status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_REQUEST_BYTES:
                return Response(status_code=413)

        # 不接受客户端提供的转发头；不转发 Cookie，不改变签名查询参数。
        headers = {"accept-encoding": "identity", "x-forwarded-proto": "https"}
        for name in ("content-type", "authorization"):
            if value := request.headers.get(name):
                headers[name] = value
        host = request.headers.get("host", "")
        if host:
            headers["host"] = host
            headers["x-forwarded-host"] = host
        query = request.scope.get("query_string", b"")
        try:
            url = upstream + request.url.path + ("?" + query.decode("ascii") if query else "")
            async with request.app.state.proxy_client.stream(
                request.method, url, content=bytes(body), headers=headers,
            ) as result:
                response_body = bytearray()
                async for chunk in result.aiter_bytes():
                    response_body.extend(chunk)
                    if len(response_body) > MAX_RESPONSE_BYTES:
                        return Response(status_code=502)
                response_headers = {
                    name: result.headers[name] for name in
                    ("content-type", "cache-control", "referrer-policy") if name in result.headers
                }
                response_headers["x-content-type-options"] = "nosniff"
                response_headers["referrer-policy"] = "no-referrer"
                return Response(content=bytes(response_body), status_code=result.status_code,
                                headers=response_headers)
        except (httpx.HTTPError, UnicodeDecodeError):
            # 不回显带消息签名、令牌等查询参数的请求 URL。
            return Response(status_code=502)

    return gateway


app = create_gateway(int(os.getenv("XIAOMAN_LOCAL_UPSTREAM_PORT", "18082")))
