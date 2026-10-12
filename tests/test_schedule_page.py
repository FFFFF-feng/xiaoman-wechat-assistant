import base64
import hashlib
import re
from unittest import TestCase

import httpx
from fastapi.testclient import TestClient

from app.local_gateway import create_gateway
from app.schedule_page import schedule_page_response


class SchedulePageTests(TestCase):
    def test_assets_are_bundled_and_exact_hashes_are_authorized(self):
        response = schedule_page_response()
        body = response.body.decode()
        self.assertNotIn('src="/static/schedule.js"', body)
        self.assertNotIn('href="/static/schedule.css"', body)
        self.assertNotIn("unsafe-inline", response.headers["content-security-policy"])
        for tag in ("style", "script"):
            asset = re.search(f"<{tag}>(.*?)</{tag}>", body, re.S)[1]
            digest = base64.b64encode(hashlib.sha256(asset.encode()).digest()).decode()
            self.assertIn(f"'sha256-{digest}'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_gateway_preserves_csp_but_still_blocks_private_routes(self):
        page = schedule_page_response()
        def upstream(request):
            return httpx.Response(200, content=page.body, headers=dict(page.headers))
        with TestClient(create_gateway(18083, schedule_only=True, transport=httpx.MockTransport(upstream))) as client:
            response = client.get("/schedule")
            self.assertEqual(response.headers["content-security-policy"], page.headers["content-security-policy"])
            for path in ("/s/" + "a" * 32, "/api/schedule/access", "/api/chat", "/api/internal/clawbot/chat"):
                self.assertEqual(client.get(path).status_code, 404)
                self.assertEqual(client.post(path).status_code, 404)
