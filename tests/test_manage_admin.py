"""Offline management contract tests: synthetic identities, no Weixin or model calls."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select, func

import test_clawbot_bridge as fixtures
import test_clawbot_enrollment as enrollment_fixtures
from app import admin_auth, manage_router, clawbot_enrollment as enrollment, clawbot_delivery, scheduler
from app.clawbot_bindings import load_bindings
from app.clawbot_identity import clawbot_owner
from app.local_gateway import allowed_path, create_gateway
from app.manage_page import manage_page_response
from app.models import ClawbotInvitation, ClawbotAdminIssue, ClawbotAdminEvent, Task
from app.schedule_access import issue_schedule_link, utc_now

PASSWORD = 'offline-test-password-not-a-real-secret'


class ManagementTests(TestCase):
    def setUp(self):
        enrollment_fixtures.EnrollmentTests.setUp(self)
        self.client.app.include_router(manage_router.router)
        self.auth_path = self.path.parent / 'admin-auth.json'
        self.auth_path.write_text(json.dumps(admin_auth.password_record(PASSWORD)), encoding='utf-8')
        self.auth_path.chmod(0o600)
        flags = patch.dict('os.environ', {'CLAWBOT_ADMIN_AUTH_FILE': str(self.auth_path.resolve()),
                                          'PUBLIC_BASE_URL': 'https://example.com'})
        flags.start(); self.addCleanup(flags.stop)
        sessions = patch.dict(admin_auth._sessions, {}, clear=True)
        sessions.start(); self.addCleanup(sessions.stop)
        attempts = patch.object(admin_auth, '_attempts', __import__('collections').deque())
        attempts.start(); self.addCleanup(attempts.stop)
        self.owner = clawbot_owner('bot', 'Alice')
        result = self.client.post('/api/manage/login', json={'username':'admin','password':PASSWORD})
        self.assertEqual(result.status_code, 200, result.text)
        self.headers = {'Authorization':'Bearer '+result.json()['session'], 'Origin':'https://example.com'}

    post = enrollment_fixtures.EnrollmentTests.post
    send = enrollment_fixtures.EnrollmentTests.send

    def issue(self, nonce='1'*32):
        return self.client.post('/api/manage/invitations',headers=self.headers,
                                json={'label':'离线体验者','request_id':nonce})

    def test_authentication_does_not_accept_bridge_or_invitation_tokens(self):
        self.assertEqual(self.client.get('/api/manage/overview').status_code,401)
        self.assertEqual(self.client.get('/api/manage/overview',headers=self.admin).status_code,401)
        token=self.issue().json()['invite']
        self.assertEqual(self.client.get('/api/manage/overview',headers={'Authorization':'Bearer '+token}).status_code,401)
        self.assertEqual(self.client.get('/api/manage/overview',headers=self.headers).status_code,200)

    def test_wrong_origin_refused_before_login_and_mutation(self):
        result=self.client.post('/api/manage/login',headers={'Origin':'https://attacker.invalid'},
                                json={'username':'admin','password':PASSWORD})
        self.assertEqual(result.status_code,403)
        headers={**self.headers,'Origin':'https://attacker.invalid'}
        self.assertEqual(self.client.post('/api/manage/invitations',headers=headers,
            json={'label':'test','request_id':'2'*32}).status_code,403)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ClawbotInvitation)),0)

    def test_logout_expiry_and_password_rotation_invalidate_session(self):
        self.assertEqual(self.client.post('/api/manage/logout',headers=self.headers,json={}).status_code,200)
        self.assertEqual(self.client.get('/api/manage/overview',headers=self.headers).status_code,401)
        token=self.client.post('/api/manage/login',json={'username':'admin','password':PASSWORD}).json()['session']
        headers={'Authorization':'Bearer '+token}
        with patch('app.admin_auth.time.monotonic',return_value=10**15):
            self.assertEqual(self.client.get('/api/manage/overview',headers=headers).status_code,401)
        token=self.client.post('/api/manage/login',json={'username':'admin','password':PASSWORD}).json()['session']
        self.auth_path.write_text(json.dumps(admin_auth.password_record('changed-offline-password')),encoding='utf-8')
        self.assertEqual(self.client.get('/api/manage/overview',headers={'Authorization':'Bearer '+token}).status_code,401)

    def test_password_not_echoed_rate_limit_and_disabled_config(self):
        for _ in range(9):
            result=self.client.post('/api/manage/login',json={'username':'admin','password':'do-not-echo-me'})
            self.assertEqual(result.status_code,401)
            self.assertNotIn('do-not-echo-me',result.text)
        self.assertEqual(self.client.post('/api/manage/login',json={'username':'admin','password':PASSWORD}).status_code,429)
        with patch.dict('os.environ',{'CLAWBOT_ENABLED':'false'}):
            self.assertEqual(self.client.get('/api/manage/overview',headers=self.headers).status_code,503)
        self.auth_path.write_text('[]',encoding='utf-8')
        self.assertEqual(self.client.get('/api/manage/overview',headers=self.headers).status_code,503)

    def test_relative_or_public_readable_config_refused(self):
        with patch.dict('os.environ',{'CLAWBOT_ADMIN_AUTH_FILE':'admin-auth.json'}):
            self.assertEqual(self.client.get('/api/manage/overview',headers=self.headers).status_code,503)
        if os.name!='nt':
            self.auth_path.chmod(0o644)
            self.assertEqual(self.client.get('/api/manage/overview',headers=self.headers).status_code,503)

    def test_overview_no_raw_identity_or_private_credentials_and_rename_keeps_owner(self):
        self.send()
        before=self.path.read_bytes()
        result=self.client.get('/api/manage/overview',headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertNotIn('Alice',result.text)
        self.assertNotIn('account_id',result.text)
        self.assertNotIn('sender_id',result.text)
        self.assertEqual(result.json()['members'][0]['authorization'],'missing')
        self.assertEqual(result.json()['members'][0]['task_count'],1)
        route='/api/manage/members/'+self.owner+'/label'
        self.assertEqual(self.client.post(route,headers=self.headers,json={'label':'体验者甲'}).status_code,200)
        self.assertEqual(self.path.read_bytes(),before)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(Task)).owner_id,self.owner)
        self.assertEqual(self.client.get('/api/manage/overview',headers=self.headers).json()['members'][0]['label'],'体验者甲')

    def test_native_status_validates_json_and_exact_identity(self):
        native=self.path.parent/'state/openclaw-weixin'
        private=self.path.parent/'state/xiaoman'
        private.mkdir(parents=True)
        flags=patch.dict('os.environ',{'CLAWBOT_BINDINGS_FILE':str(private/'bindings.json')})
        flags.start();self.addCleanup(flags.stop)
        native.mkdir(exist_ok=True);(native/'accounts').mkdir(exist_ok=True)
        (native/'accounts.json').write_text(json.dumps(['bot']))
        account=native/'accounts/bot.json'
        for value,expected in [([], 'unknown'),({'userId':'Other','token':'test-only'},'mismatch'),
                ({'userId':'Alice','token':True},'mismatch'),({'userId':'Alice','token':'test-only'},'ready')]:
            account.write_text(json.dumps(value))
            self.assertEqual(manage_router.native_state(fixtures.BINDINGS[0]),expected)

    def test_issue_atomic_dedup_and_history_without_secret(self):
        issued=self.issue();self.assertEqual(issued.status_code,200,issued.text)
        value=issued.json()
        self.assertEqual(len(value['invite']),43)
        self.assertEqual(self.issue().status_code,409)
        with self.sessions() as db:
            row=db.get(ClawbotInvitation,value['id'])
            self.assertEqual(row.token_hash,enrollment.digest(value['invite']))
            self.assertLess(abs((row.expires_at-row.created_at).total_seconds()-1800),1)
            self.assertEqual(db.scalar(select(func.count()).select_from(ClawbotAdminIssue)),1)
        for route in ('/api/manage/invitations','/api/manage/events','/api/manage/overview'):
            history=self.client.get(route,headers=self.headers)
            self.assertNotIn(value['invite'],history.text)
            self.assertNotIn('token_hash',history.text)
            self.assertNotIn('qr_image',history.text)
            self.assertNotIn('lease',history.text)

    def test_cancel_stops_worker_and_connected_requires_member_revoke(self):
        value=self.issue().json()
        invitation={'Authorization':'Bearer '+value['invite']}
        self.assertEqual(self.post('/api/join/start',headers=invitation).status_code,200)
        job=self.post('/api/internal/clawbot/enrollment/claim').json()['job']
        self.assertEqual(self.client.post('/api/manage/invitations/'+str(value['id'])+'/cancel',headers=self.headers,json={}).status_code,200)
        result=self.post(f"/api/internal/clawbot/enrollment/{job['id']}/complete",
                         {'lease':job['lease'],'account_id':'bot-two','sender_id':'Bob'})
        self.assertEqual(result.status_code,409)
        self.assertEqual(len(load_bindings()),1)
        with self.sessions() as db:
            row=db.get(ClawbotInvitation,value['id']);row.state='connected';row.owner_id=self.owner;db.commit()
        self.assertEqual(self.client.post('/api/manage/invitations/'+str(value['id'])+'/cancel',headers=self.headers,json={}).status_code,409)

    def test_revoke_keeps_tasks_invalidates_schedule_and_unstarted_delivery(self):
        self.send()
        with self.sessions() as db:
            link=issue_schedule_link(db,self.owner,'https://example.com')
            task=db.scalar(select(Task));task.due_at=clawbot_delivery.local_now()-timedelta(seconds=1);db.commit()
        with patch('app.scheduler.SessionLocal',self.sessions):scheduler.check_due_tasks()
        item=self.post('/api/internal/clawbot/outbox/claim').json()['delivery']
        self.assertEqual(self.client.post('/api/manage/members/'+self.owner+'/revoke',headers=self.headers,json={}).status_code,200)
        self.assertEqual(load_bindings(),[])
        self.assertEqual(self.send(mid='after-revoke').status_code,403)
        self.assertEqual(self.client.get('/api/schedule/tasks',params={'start':'2026-10-01','end':'2026-11-01'},
            headers={'Authorization':'Bearer '+link.split('access=')[1]}).status_code,401)
        self.assertEqual(self.post(f"/api/internal/clawbot/outbox/{item['id']}/start",{'claim_token':item['claim_token']}).status_code,409)
        with self.sessions() as db:self.assertIsNotNone(db.scalar(select(Task)))

    def test_bad_member_or_extra_fields_never_mutate(self):
        before=self.path.read_bytes()
        self.assertEqual(self.client.post('/api/manage/members/clawbot:'+'0'*64+'/revoke',headers=self.headers,json={}).status_code,404)
        for label in (' ', '\n', 'x'*61):
            self.assertEqual(self.client.post('/api/manage/members/'+self.owner+'/label',headers=self.headers,json={'label':label}).status_code,400)
        self.assertEqual(self.client.post('/api/manage/invitations',headers=self.headers,
            json={'label':'test','request_id':'3'*32,'sender_id':'injected'}).status_code,400)
        self.assertEqual(self.path.read_bytes(),before)

    def test_capacity_expiry_and_body_size(self):
        with patch.dict('os.environ',{'CLAWBOT_MAX_USERS':'1'}):self.assertEqual(self.issue().status_code,409)
        with self.sessions() as db:
            db.add(ClawbotInvitation(token_hash='a'*64,label='expired',state='issued',
                                    created_at=utc_now()-timedelta(hours=1),expires_at=utc_now()-timedelta(minutes=1)));db.commit()
        self.assertEqual(self.client.get('/api/manage/invitations',headers=self.headers).json()['invitations'][0]['state'],'expired')
        result=self.client.post('/api/manage/login',content='private-invalid-json')
        self.assertEqual(result.status_code,400);self.assertNotIn('private-invalid-json',result.text)
        self.assertEqual(self.client.post('/api/manage/login',content=b'x'*4097).status_code,413)

    def test_page_csp_no_storage_or_credentials_in_static_html(self):
        result=manage_page_response();page=result.body.decode()
        self.assertIn('sha256-',result.headers['content-security-policy'])
        self.assertNotIn('unsafe-inline',result.headers['content-security-policy'])
        self.assertEqual(result.headers['cache-control'],'no-store')
        for forbidden in ('localStorage','sessionStorage',PASSWORD,'Alice','bridge.key','innerHTML'):
            self.assertNotIn(forbidden,page)

    def test_public_proxy_strict_paths_and_origin_no_cookie_or_cors_forwarding(self):
        owner=self.owner
        paths=[('/manage','GET'),('/api/manage/login','POST'),('/api/manage/overview','GET'),
               ('/api/manage/invitations','POST'),('/api/manage/members/'+owner+'/revoke','POST')]
        for path,method in paths:
            self.assertTrue(allowed_path(path,method,schedule_only=True))
            self.assertFalse(allowed_path(path,method))
        for path,method in [('/api/manage/overview','POST'),('/api/manage/login','GET'),('/api/manage/login','OPTIONS'),
                            ('/api/manage/','GET'),('/api/manage/members/Alice/revoke','POST'),('/api/internal/clawbot/health','GET')]:
            self.assertFalse(allowed_path(path,method,schedule_only=True))
        requests=[]
        def upstream(request):
            requests.append(request)
            return httpx.Response(200,json={'ok':True},headers={'Access-Control-Allow-Origin':'*'})
        with TestClient(create_gateway(18083,schedule_only=True,transport=httpx.MockTransport(upstream))) as client:
            result=client.post('/api/manage/login',headers={'Origin':'https://example.com','Cookie':'private=value'},json={})
        self.assertEqual(requests[0].headers['origin'],'https://example.com')
        self.assertNotIn('cookie',requests[0].headers)
        self.assertNotIn('access-control-allow-origin',result.headers)
        self.assertEqual(result.headers['cache-control'],'no-store')


class AdminSetupTests(TestCase):
    def test_setup_is_owned_private_non_overwriting_and_rotation_preserves_env(self):
        script=Path(__file__).resolve().parents[1]/'scripts/setup-clawbot-admin.py'
        spec=importlib.util.spec_from_file_location('admin_setup_test',script)
        setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();private=root/'state/xiaoman';private.mkdir(parents=True)
            (private/'.xiaoman-bridge').write_text('xiaoman-server-runtime-v1\n')
            (private/'runtime.env').write_text('MODEL_ENABLED=false\nCLAWBOT_ENABLED=true\n')
            (root/'state/openclaw.json').write_text(json.dumps({'plugins':{'entries':{'xiaoman-bridge':
                {'enabled':True,'config':{'bindingsFile':str(private/'bindings.json')}}}}}))
            result=setup.configure(root)
            self.assertTrue(result.exists())
            self.assertIn('CLAWBOT_ADMIN_AUTH_FILE=',(private/'runtime.env').read_text())
            self.assertIn('MODEL_ENABLED=false',(private/'runtime.env').read_text())
            first=(private/'admin-auth.json').read_bytes()
            with self.assertRaises(ValueError):setup.configure(root)
            self.assertEqual((private/'admin-auth.json').read_bytes(),first)
            rotated=setup.configure(root,rotate=True)
            self.assertNotEqual(result,rotated)
            self.assertNotEqual((private/'admin-auth.json').read_bytes(),first)
            self.assertEqual((private/'runtime.env').read_text().count('CLAWBOT_ADMIN_AUTH_FILE='),1)
            if os.name!='nt':
                self.assertEqual(result.stat().st_mode&0o777,0o600)
                self.assertEqual((private/'admin-auth.json').stat().st_mode&0o777,0o600)
