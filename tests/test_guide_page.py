import base64
import hashlib
import re
from unittest import TestCase
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.guide_page import guide_page_response, router
from app.local_gateway import create_gateway, allowed_path


class GuidePageTests(TestCase):
    def test_public_page_has_bundled_hash_csp_and_no_identity_or_network_requests(self):
        response = guide_page_response(); body = response.body.decode()
        self.assertNotIn('unsafe-inline', response.headers['content-security-policy'])
        self.assertIn("connect-src 'none'", response.headers['content-security-policy'])
        for tag in ('style', 'script'):
            asset = re.search(f'<{tag}>(.*?)</{tag}>', body, re.S)[1]
            digest = base64.b64encode(hashlib.sha256(asset.encode()).digest()).decode()
            self.assertIn(f"'sha256-{digest}'", response.headers['content-security-policy'])
        self.assertIn('不会自动修改微信资料', body)
        self.assertIn('清空此功能消息记录', body)
        self.assertNotIn('fetch(', body)
        self.assertIn('一分钟后提醒我喝水', body)
        self.assertIn('帮我打开一下我的日程', body)
        self.assertIn('id="updates"', body)
        self.assertIn('关闭表情包', body)
        self.assertEqual(response.headers['referrer-policy'], 'no-referrer')

    def test_avatar_and_page_served_through_gateway_without_expanding_private_access(self):
        app = FastAPI(); app.include_router(router)
        with TestClient(app) as upstream:
            def fetch(request):
                result = upstream.get(request.url.path)
                return httpx.Response(result.status_code, content=result.content, headers=dict(result.headers))
            with TestClient(create_gateway(18083, schedule_only=True, transport=httpx.MockTransport(fetch))) as proxy:
                self.assertEqual(proxy.get('/guide').status_code, 200)
                image = proxy.get('/static/xiaoman-avatar.jpg')
                self.assertEqual(image.status_code, 200)
                self.assertTrue(image.content.startswith(b'\x89PNG\r\n\x1a\n'))
                self.assertEqual(image.headers['content-type'], 'image/png')
                self.assertEqual(image.headers['cache-control'], 'no-store')
                self.assertIn('.png', image.headers['content-disposition'])
                for path in ('/guide', '/static/xiaoman-avatar.jpg'):
                    self.assertEqual(proxy.post(path).status_code, 404)
        self.assertFalse(allowed_path('/static/guide.js', 'GET', schedule_only=True))
        self.assertFalse(allowed_path('/api/internal/clawbot/welcome/claim', 'POST', schedule_only=True))
