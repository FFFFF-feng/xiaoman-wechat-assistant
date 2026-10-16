import unittest

import httpx
from fastapi.testclient import TestClient

from app.local_gateway import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, create_gateway


class LocalGatewayTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def upstream(request):
            self.requests.append(request)
            return httpx.Response(200, text="success", headers={"content-type": "application/xml"})

        self.client = TestClient(create_gateway(18082, transport=httpx.MockTransport(upstream)))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_debug_endpoints_and_unlisted_assets_are_not_forwarded(self):
        for path in ("/", "/health", "/docs", "/openapi.json", "/api/chat", "/api/tasks",
                     "/api/notifications", "/static/app.js", "/.env", "/api/schedule/other",
                     "/api/channels/wechat/callback/", "/static/schedule.js/other"):
            for method in ("GET", "POST"):
                self.assertEqual(self.client.request(method, path).status_code, 404, path)
        self.assertEqual(self.requests, [])

    def test_callback_signature_query_and_xml_are_preserved(self):
        path = "/api/channels/wechat/callback?msg_signature=abc&timestamp=1&nonce=n&encrypt_type=aes"
        response = self.client.post(path, content=b"<xml><Encrypt>body</Encrypt></xml>",
                                    headers={"Content-Type": "application/xml", "Cookie": "private=value",
                                             "X-Forwarded-Host": "attacker.invalid", "X-Forwarded-Proto": "http"})
        self.assertEqual(response.status_code, 200)
        req = self.requests[-1]
        self.assertEqual(req.url.host, "127.0.0.1")
        self.assertEqual(req.url.port, 18082)
        self.assertEqual(req.url.query, b"msg_signature=abc&timestamp=1&nonce=n&encrypt_type=aes")
        self.assertEqual(req.content, b"<xml><Encrypt>body</Encrypt></xml>")
        self.assertEqual(req.headers["x-forwarded-host"], "testserver")
        self.assertEqual(req.headers["x-forwarded-proto"], "https")
        self.assertNotIn("cookie", req.headers)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_schedule_bearer_is_forwarded_and_other_methods_are_blocked(self):
        response = self.client.get("/api/schedule/tasks?start=2026-10-04&end=2026-10-04",
                                   headers={"Authorization": "Bearer test-only-value"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[-1].headers["authorization"], "Bearer test-only-value")
        self.assertEqual(self.client.post("/api/schedule/tasks/12/cancel").status_code, 200)
        self.assertEqual(self.client.post("/api/schedule/tasks", json={}).status_code, 200)
        self.assertEqual(self.client.post("/api/schedule/tasks/12/edit", json={}).status_code, 200)
        previous = len(self.requests)
        for path, method in (("/api/schedule/tasks", "DELETE"), ("/schedule", "POST"),
                             ("/api/schedule/tasks/12/cancel", "GET"), ("/api/channels/wechat/callback", "PUT")):
            self.assertEqual(self.client.request(method, path).status_code, 404)
        self.assertEqual(len(self.requests), previous)

    def test_oversized_body_is_not_forwarded(self):
        result = self.client.post("/api/channels/wechat/callback", content=b"x" * (MAX_REQUEST_BYTES + 1))
        self.assertEqual(result.status_code, 413)
        self.assertEqual(self.requests, [])

    def test_upstream_failure_is_generic_and_has_no_retry(self):
        attempts = []

        def unavailable(request):
            attempts.append(request)
            raise httpx.ConnectError("private query must not leak", request=request)

        with TestClient(create_gateway(18082, transport=httpx.MockTransport(unavailable))) as client:
            response = client.get("/api/channels/wechat/callback?signature=private")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.content, b"")
        self.assertEqual(len(attempts), 1)

    def test_oversized_response_is_rejected(self):
        with TestClient(create_gateway(18082, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        ))) as client:
            self.assertEqual(client.get("/schedule").status_code, 502)


if __name__ == "__main__":
    unittest.main()
