"""Synthetic identity, in-memory DB, no native Weixin sends or model requests."""
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import patch
from sqlalchemy import func, select

from app import clawbot_stickers as stickers
from app.clawbot_identity import clawbot_owner
from app.models import ClawbotStickerDelivery as Delivery, ClawbotStickerPreference as Preference, Task, Reminder
from app.models import ClawbotStickerStyle as Style
from app.sticker_catalog import PHOENIX, FLUENT, SCENES
from app.local_gateway import allowed_path
import test_clawbot_bridge as fixtures


class StickerTests(TestCase):
    send = fixtures.ClawbotBridgeTests.send

    def setUp(self):
        fixtures.ClawbotBridgeTests.setUp(self)
        self.now = datetime(2026,10,18,1,0)  # 09:00 Beijing, independent of host timezone.
        for p in [patch.dict('os.environ',{'CLAWBOT_STICKERS_ENABLED':'true'}),
                  patch('app.clawbot_stickers.utc_now',side_effect=lambda:self.now)]:
            p.start(); self.addCleanup(p.stop)
        self.headers={'Authorization':'Bearer '+fixtures.TOKEN}
        self.alice=clawbot_owner('bot','Alice'); self.bob=clawbot_owner('bot','Bob')

    def post(self,path,body=None):
        return self.client.post('/api/internal/clawbot/stickers'+path,headers=self.headers,json=body or {})

    def claim(self):
        r=self.post('/claim'); self.assertEqual(r.status_code,200,r.text); return r.json()['delivery']

    def start(self,item):
        return self.post(f"/{item['id']}/start",{'claim_token':item['claim_token']})

    def result(self,item,outcome='accepted',**fields):
        return self.post(f"/{item['id']}/result",{'claim_token':item['claim_token'],'outcome':outcome,**fields})

    def count(self,model=Delivery):
        with self.sessions() as db: return db.scalar(select(func.count()).select_from(model))

    def daily_ready(self):
        with self.sessions() as db:
            db.add(Preference(owner_id=self.alice,daily_enabled=True,daily_started_at=self.now-timedelta(days=1),last_active_at=self.now))
            db.commit(); stickers.queue_daily(db,fixtures.BINDINGS)

    def test_assets_are_present_integrity_checked_and_not_arbitrary_paths(self):
        for name in stickers.CAPTIONS: self.assertTrue(stickers.available(name),name)
        for name in ('../../.env','https://example.com/image','unknown',None):
            self.assertFalse(stickers.available(name))

    def test_manual_receipt_is_idempotent_owner_bound_and_not_a_task(self):
        first=self.send('给我收到表情包')
        self.assertEqual(first.status_code,200,first.text)
        self.assertIn('发送队列',first.json()['reply'])
        self.assertEqual(first.json(),self.send('给我收到表情包').json())
        self.assertEqual(self.count(),1); self.assertEqual(self.count(Task),0); self.assertEqual(self.count(Reminder),0)
        item=self.claim(); self.assertEqual(item['sticker_id'],'received'); self.assertEqual(item['sender_id'],'Alice')
        self.assertNotIn('mediaUrl',item); self.assertNotIn('file',item)
        self.assertIsNone(self.claim())

    def test_defaults_and_preferences_do_not_leak_between_users(self):
        self.send('表情设置')
        with self.sessions() as db:
            pref=db.get(Preference,self.alice); self.assertTrue(pref.daily_enabled); self.assertTrue(pref.chat_enabled)
            self.assertEqual(pref.daily_started_at,self.now)
        self.send('关闭表情包',mid='2'); self.send('表情设置',user='Bob',mid='3')
        with self.sessions() as db:
            self.assertFalse(db.get(Preference,self.alice).daily_enabled)
            self.assertTrue(db.get(Preference,self.bob).daily_enabled)

    def test_natural_manual_aliases_and_negated_or_quoted_commands(self):
        self.send('发个表情包'); self.send('发个早安表情包',mid='2')
        with self.sessions() as db:
            self.assertEqual([r.sticker_id for r in db.scalars(select(Delivery).order_by(Delivery.id))],['received','morning'])
        self.send('关闭日常表情',mid='off')
        self.send('不要开启日常表情',mid='3'); self.send('“开启日常表情”',mid='4')
        with self.sessions() as db: self.assertFalse(db.get(Preference,self.alice).daily_enabled)

    def test_global_disabled_creates_no_delivery_or_opt_in(self):
        with patch.dict('os.environ',{'CLAWBOT_STICKERS_ENABLED':'false'}):
            r=self.send('开启日常表情'); self.assertIn('尚未开放',r.json()['reply'])
            self.assertEqual(self.post('/claim').status_code,503)
        self.assertEqual(self.count(),0); self.assertEqual(self.count(Preference),0)

    def test_missing_asset_does_not_claim_success(self):
        with patch('app.clawbot_stickers.available',return_value=False):
            self.assertIn('暂未开启',self.send('开启聊天表情').json()['reply'])
            self.assertIn('没有安排发送',self.send('给我收到表情包',mid='2').json()['reply'])
        self.assertEqual(self.count(),0)

    def test_manual_has_no_count_or_cooldown_limit_but_still_expires(self):
        for i in range(8):
            self.assertIn('发送队列',self.send('给我收到表情包',mid=str(i)).json()['reply'])
        self.assertEqual(self.count(),8)
        self.now+=timedelta(minutes=6)
        self.assertIsNone(self.claim())

    def test_daily_once_per_beijing_date_and_no_same_day_opt_in(self):
        self.send('开启日常表情')
        with self.sessions() as db: stickers.queue_daily(db,fixtures.BINDINGS)
        self.assertEqual(self.count(),0)
        self.now+=timedelta(days=1)
        with self.sessions() as db:
            stickers.queue_daily(db,fixtures.BINDINGS); stickers.queue_daily(db,fixtures.BINDINGS)
        self.assertEqual(self.count(),len(fixtures.BINDINGS))
        item=self.claim(); self.assertEqual(item['sticker_id'],'morning')

    def test_repeated_daily_enable_preserves_start_date_and_does_not_promise_tomorrow_again(self):
        self.daily_ready()
        with self.sessions() as db: original=db.get(Preference,self.alice).daily_started_at
        self.assertIn('已经开启',self.send('开启日常表情').json()['reply'])
        with self.sessions() as db: self.assertEqual(db.get(Preference,self.alice).daily_started_at,original)
        self.assertEqual(self.count(),1)

    def test_daily_skips_missed_window_and_revoked_users_without_activity_gate(self):
        with self.sessions() as db:
            db.add(Preference(owner_id=self.alice,daily_enabled=True,daily_started_at=self.now-timedelta(days=2),last_active_at=self.now-timedelta(hours=25)))
            db.commit()
            stickers.queue_daily(db,[]); self.assertEqual(self.count(),0)
            self.now=self.now.replace(minute=15); stickers.queue_daily(db,fixtures.BINDINGS); self.assertEqual(self.count(),0)
            self.now=self.now.replace(hour=14); stickers.queue_daily(db,fixtures.BINDINGS); self.assertEqual(self.count(),0)
            self.now=self.now.replace(hour=1,minute=0)
            stickers.queue_daily(db,fixtures.BINDINGS)
        self.assertEqual(self.count(),1)
        item=self.claim(); self.assertEqual(item['sender_id'],'Alice')
        self.assertEqual(self.start(item).status_code,200)

    def test_disable_after_claim_blocks_start(self):
        self.daily_ready(); item=self.claim()
        self.send('关闭日常表情')
        self.assertEqual(self.start(item).status_code,409); self.assertIsNone(self.claim())

    def test_opt_out_is_persisted_even_while_deployment_switch_is_off(self):
        self.send('开启日常表情'); self.send('开启聊天表情',mid='2')
        self.send('给我收到表情包',mid='3'); item=self.claim()
        with patch.dict('os.environ',{'CLAWBOT_STICKERS_ENABLED':'false'}):
            self.assertIn('都已关闭',self.send('不要再发表情',mid='4').json()['reply'])
        with self.sessions() as db:
            pref=db.get(Preference,self.alice)
            self.assertFalse(pref.daily_enabled); self.assertFalse(pref.chat_enabled)
        self.assertEqual(self.start(item).status_code,409); self.assertIsNone(self.claim())

    def test_revocation_and_global_off_block_begin(self):
        self.send('给我收到表情包'); item=self.claim()
        with patch('app.clawbot_router.load_bindings',return_value=[]):
            self.assertEqual(self.start(item).status_code,409)
        self.send('给我收到表情包',mid='2'); item=self.claim()
        with patch.dict('os.environ',{'CLAWBOT_STICKERS_ENABLED':'false'}):
            self.assertEqual(self.start(item).status_code,503)

    def test_leases_unknown_outcomes_no_resend_and_accepted_ack_idempotency(self):
        self.send('给我收到表情包'); first=self.claim()
        self.now+=timedelta(seconds=46); second=self.claim()
        self.assertNotEqual(first['claim_token'],second['claim_token'])
        self.assertEqual(self.start(first).status_code,409)
        self.assertEqual(self.start(second).status_code,200)
        self.assertEqual(self.start(second).status_code,409)
        self.now+=timedelta(seconds=121); self.assertIsNone(self.claim())
        with self.sessions() as db: self.assertEqual(db.get(Delivery,second['id']).status,'unknown')
        self.assertEqual(self.result(second,message_id='synthetic-platform-id').status_code,200)
        self.assertEqual(self.result(second,message_id='synthetic-platform-id').status_code,200)
        self.assertIsNone(self.claim())

    def test_empty_receipt_rejected_failed_send_not_requeued(self):
        self.send('给我收到表情包'); item=self.claim(); self.start(item)
        self.assertEqual(self.result(item).status_code,422)
        self.assertEqual(self.result(item,'failed',error_code='empty_receipt').status_code,200)
        self.assertIsNone(self.claim())

    def test_chat_reaction_is_default_on_without_cooldown_or_daily_cap(self):
        self.send('谢谢你'); self.assertEqual(self.count(),1)
        for i in range(8):
            self.send('谢谢你',mid='reaction-'+str(i))
        self.assertEqual(self.count(),9)
        self.send('谢谢你',mid='reaction-7'); self.assertEqual(self.count(),9)
        self.send('关闭聊天表情',mid='off')
        self.send('晚安',mid='night'); self.assertEqual(self.count(),9)
        self.send('开启表情包',mid='restore')
        self.send('晚安',mid='night-restored'); self.assertEqual(self.count(),10)

    def test_default_initializes_existing_bindings_and_never_reopens_opt_out(self):
        self.now=self.now.replace(hour=4)  # Initialize outside the greeting window.
        with self.sessions() as db:
            stickers.queue_daily(db,fixtures.BINDINGS)
            self.assertTrue(db.get(Preference,self.bob).chat_enabled)
        self.send('不要再发表情包')
        self.now+=timedelta(days=2)
        self.send('表情设置',mid='later')
        with self.sessions() as db:
            stickers.queue_daily(db,fixtures.BINDINGS)
            pref=db.get(Preference,self.alice)
            self.assertFalse(pref.chat_enabled); self.assertFalse(pref.daily_enabled)
        self.send('早安',mid='morning'); self.assertEqual(self.count(),0)
        self.send('恢复表情包',mid='restore')
        with self.sessions() as db:
            pref=db.get(Preference,self.alice)
            self.assertTrue(pref.chat_enabled); self.assertTrue(pref.daily_enabled)
            self.assertEqual(pref.daily_started_at,self.now)

    def test_feature_defaults_on_but_requires_bridge_and_honors_admin_off(self):
        with patch.dict('os.environ'):
            import os
            os.environ.pop('CLAWBOT_STICKERS_ENABLED',None)
            self.assertTrue(stickers.enabled())
            os.environ['CLAWBOT_ENABLED']='false'
            self.assertFalse(stickers.enabled())
            os.environ['CLAWBOT_ENABLED']='true'
            os.environ['CLAWBOT_STICKERS_ENABLED']='false'
            self.assertFalse(stickers.enabled())

    def test_chat_import_does_not_execute_quoted_control_commands(self):
        self.send('关闭表情包',mid='off')
        self.send('整理聊天记录')
        self.send('开启日常表情',mid='2')
        with self.sessions() as db: self.assertFalse(db.get(Preference,self.alice).daily_enabled)
        self.assertEqual(self.count(),0)

    def test_internal_endpoints_remain_authenticated_and_not_public(self):
        self.assertEqual(self.client.post('/api/internal/clawbot/stickers/claim').status_code,401)
        for path in ('/api/internal/clawbot/stickers/claim','/api/internal/clawbot/stickers/1/start','/assets/stickers/phoenix-d/morning.png'):
            self.assertFalse(allowed_path(path,'POST',schedule_only=True))
            self.assertFalse(allowed_path(path,'GET',schedule_only=True))

    def test_expanded_emotions_ordinary_chat_variety_and_stop_are_respected(self):
        for i,text in enumerate(['哈哈哈今天好开心','我有点难过','今天有点累','出门了','等等我','随便聊几句吧','午饭吃了面条']):
            self.send(text,mid='new-chat-'+str(i))
        self.assertEqual(self.count(),7)
        with self.sessions() as db:
            rows=db.scalars(select(Delivery).order_by(Delivery.id)).all()
            self.assertIn(rows[0].sticker_id,stickers.SCENES['happy'])
            self.assertIn(rows[1].sticker_id,stickers.SCENES['comfort'])
            self.assertIn(rows[2].sticker_id,stickers.SCENES['tired'])
            self.assertNotEqual(rows[-1].sticker_id,rows[-2].sticker_id)
        self.send('关闭表情包',mid='off')
        self.send('哈哈哈',mid='closed'); self.assertEqual(self.count(),7)

    def test_all_expanded_assets_can_be_requested_and_context_avoids_bad_reactions(self):
        for i,name in enumerate(['抱抱','哈哈哈','在呢','委屈','好哒','辛苦啦','等等我','出发']):
            self.assertIn('发送队列',self.send('给我'+name+'表情包',mid='asset-'+str(i)).json()['reply'])
        self.assertEqual(self.count(),8)
        for text in ['急救应该怎么做','他去世了','解释一下这个函数','“哈哈哈”是引用内容','请不要在回复里发表情']:
            self.assertIsNone(stickers.choose_sticker(text,'synthetic'))

    def test_natural_opt_out_is_persistent_and_quotes_do_not_trigger_it(self):
        self.send('“别再给我发表情包了”')
        with self.sessions() as db: self.assertTrue(db.get(Preference,self.alice).chat_enabled)
        self.send('以后不要再给我发表情包了',mid='off')
        self.send('哈哈哈今天真开心',mid='later')
        with self.sessions() as db:
            pref=db.get(Preference,self.alice)
            self.assertFalse(pref.chat_enabled); self.assertFalse(pref.daily_enabled)
        self.assertEqual(self.count(),0)

    def test_asset_preflight_failure_is_terminal_without_claim_retry_loop(self):
        self.send('给我抱抱表情包'); item=self.claim()
        self.assertEqual(self.result(item,'failed',error_code='asset_unavailable').status_code,200)
        self.assertIsNone(self.claim())
        self.assertEqual(self.start(item).status_code,409)

    def test_natural_sticker_request_does_not_defer_to_inaccurate_model_claims(self):
        with patch('app.clawbot_router.handle_chat_message') as model:
            response=self.send('咋不给我发发表情包')
            self.assertIn('发送队列',response.json()['reply'])
            self.assertNotIn('只能发文字',response.json()['reply'])
            model.assert_not_called()
        self.assertEqual(self.claim()['sticker_id'],'hug')

    def test_reviewed_assets_match_pinned_sources_license_and_python_node_allowlists(self):
        import hashlib
        import json
        from pathlib import Path
        import re
        from PIL import Image
        source = json.loads((stickers.ASSETS.parent / 'fluent-source.json').read_text(encoding='utf-8'))
        catalog = json.loads((stickers.ASSETS / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(len(catalog), 38)
        self.assertEqual({entry['id'] for entry in source['assets']}, set(FLUENT))
        self.assertEqual(set(catalog), set(stickers.CAPTIONS))
        node = (Path(__file__).resolve().parents[1] / 'integrations/openclaw-xiaoman/sticker-assets.js').read_text()
        fixed_ids = set(re.findall(r"'([a-z_]+)'", node.split('const IDS = new Set(')[1].split(']);')[0]))
        self.assertEqual(fixed_ids, set(catalog))
        license_data = (stickers.ASSETS / 'licenses/fluent-emoji-LICENSE.txt').read_bytes()
        blob = lambda data: hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        self.assertEqual(blob(license_data), source['license_git_blob_sha'])
        self.assertIn(b'Copyright (c) Microsoft Corporation.', license_data)
        for entry in source['assets']:
            path = stickers.ASSETS / (entry['id'] + '.png')
            self.assertEqual(blob(path.read_bytes()), entry['git_blob_sha'])
            meta = catalog[entry['id']]
            self.assertEqual(meta['license'], 'MIT')
            self.assertIn(source['commit'], meta['source_url'])
            with Image.open(path) as image:
                self.assertEqual((image.format, image.size, image.mode), ('PNG', (256, 256), 'RGBA'))
                image.verify()

    def test_style_is_owner_scoped_persistent_and_does_not_reopen_opt_out(self):
        self.send('关闭表情包')
        self.send('只用3D表情', mid='style')
        self.send('哈哈哈', mid='quiet')
        self.send('表情设置', user='Bob', mid='bob')
        with self.sessions() as db:
            self.assertEqual(stickers.style_for(db, self.alice), 'fluent')
            self.assertEqual(stickers.style_for(db, self.bob), 'mixed')
            self.assertFalse(db.get(Preference, self.alice).chat_enabled)
            self.assertFalse(db.get(Preference, self.alice).daily_enabled)
        self.assertEqual(self.count(), 0)
        self.assertIn('只用3D', self.send('表情设置', mid='view').json()['reply'])
        self.send('开启聊天表情', mid='on')
        self.send('哈哈哈', mid='happy')
        self.assertIn(self.claim()['sticker_id'], set(FLUENT) & set(SCENES['happy']))

    def test_style_survives_new_database_engine_without_altering_existing_tables(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.database import Base
        with TemporaryDirectory(prefix='xiaoman-style-') as directory:
            url = 'sqlite:///' + str(Path(directory) / 'synthetic.sqlite')
            old = create_engine(url)
            Base.metadata.create_all(old, tables=[Preference.__table__, Task.__table__])
            with Session(old) as db:
                db.add(Preference(owner_id=self.alice, chat_enabled=False, daily_enabled=False, last_active_at=self.now))
                db.commit()
            Base.metadata.create_all(old)
            with Session(old) as db:
                stickers.command(db, self.alice, '只用3D表情', 'synthetic')
            old.dispose()
            fresh = create_engine(url)
            try:
                Base.metadata.create_all(fresh)
                with Session(fresh) as db:
                    self.assertEqual(stickers.style_for(db, self.alice), 'fluent')
                    self.assertFalse(db.get(Preference, self.alice).chat_enabled)
            finally:
                fresh.dispose()

    def test_style_switch_invalidates_claimed_automatic_images_not_manual_requests(self):
        self.send('你好')
        automatic = self.claim()
        self.send('给我抱抱表情包', mid='manual')
        self.send('只用3D表情', mid='style')
        self.assertEqual(self.start(automatic).status_code, 409)
        manual = self.claim()
        self.assertEqual(manual['sticker_id'], 'hug')
        self.assertEqual(self.start(manual).status_code, 200)
        self.send('你好', mid='next')
        self.send('只用3D表情', mid='same-style')
        self.assertIn(self.claim()['sticker_id'], FLUENT)

    def test_switch_saves_while_admin_disabled_and_quotes_import_do_not_change_style(self):
        with patch.dict('os.environ', {'CLAWBOT_STICKERS_ENABLED': 'false'}):
            self.send('只用3D表情')
        self.send('“只用小凤鸟表情”', mid='quoted')
        self.send('如果我说只用小凤鸟表情呢', mid='conditional')
        self.send('整理聊天记录', mid='import')
        self.send('混合表情', mid='quoted-in-import')
        with self.sessions() as db:
            self.assertEqual(stickers.style_for(db, self.alice), 'fluent')
        self.assertEqual(self.count(), 0)

    def test_scene_selection_covers_both_families_and_avoids_recent_images(self):
        samples = [stickers.choose_sticker('谢谢你', str(i)) for i in range(40)]
        self.assertTrue(set(samples) & set(PHOENIX))
        self.assertTrue(set(samples) & set(FLUENT))
        for text, scene in [('好开心', 'happy'), ('我不开心哈哈', 'comfort'), ('好累', 'tired'), ('呜呜', 'sad')]:
            for style, family in [('phoenix', PHOENIX), ('fluent', FLUENT)]:
                for i in range(12):
                    chosen = stickers.choose_sticker(text, str(i), style=style)
                    self.assertIn(chosen, set(SCENES[scene]) & set(family))
        recent = SCENES['neutral'][:3]
        self.assertNotIn(stickers.choose_sticker('午饭吃了面条', 'avoid', recent=recent), recent)
        self.assertIsNone(stickers.choose_sticker('哈哈帮我解释这个函数', 'code'))

    def test_missing_external_family_falls_back_only_in_mixed_mode(self):
        original = stickers.available
        with patch('app.clawbot_stickers.available', side_effect=lambda s: s in PHOENIX and original(s)):
            self.assertIn('开启', self.send('开启聊天表情').json()['reply'])
            self.send('谢谢你', mid='thanks')
            self.assertIn(self.claim()['sticker_id'], PHOENIX)
            self.send('只用3D表情', mid='fluent')
            self.send('谢谢你', mid='unavailable')
            self.assertIsNone(self.claim())
            self.assertIn('没有安排发送', self.send('给我抱抱表情包', mid='request').json()['reply'])
            self.assertIn('暂未开启', self.send('开启聊天表情', mid='on').json()['reply'])
        self.send('混合表情', mid='mixed')
        self.send('关闭日常表情', mid='daily-off')
        with patch('app.clawbot_stickers.available', side_effect=lambda s: s == 'thanks'):
            self.assertIn('暂未开启', self.send('开启日常表情', mid='daily-no-morning').json()['reply'])
        with self.sessions() as db:
            self.assertFalse(db.get(Preference, self.alice).daily_enabled)

    def test_explicit_external_requests_and_native_equivalents_in_fluent_mode(self):
        self.send('来张猫猫表情')
        self.assertEqual(self.claim()['sticker_id'], 'fluent_cat_hello')
        self.send('给我3D抱抱表情包', mid='3d')
        self.assertEqual(self.claim()['sticker_id'], 'fluent_hug')
        self.send('只用3D表情', mid='style')
        self.send('给我收到表情包', mid='receipt')
        self.assertIn(self.claim()['sticker_id'], set(SCENES['okay']) & set(FLUENT))
        self.send('给我加油表情包', mid='cheer')
        self.assertIn(self.claim()['sticker_id'], set(SCENES['cheer']) & set(FLUENT))

    def test_manual_next_image_respects_style_and_replay_remains_idempotent(self):
        self.send('只用3D表情')
        for i in range(6):
            self.send('换张表情', mid='next-'+str(i))
        self.send('换张表情', mid='next-5')
        with self.sessions() as db:
            rows = list(db.scalars(select(Delivery).order_by(Delivery.id)))
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(row.sticker_id in FLUENT for row in rows))
            self.assertTrue(all(a.sticker_id != b.sticker_id for a, b in zip(rows, rows[1:])))

    def test_daily_follows_fluent_style_and_style_change_does_not_duplicate_greeting(self):
        self.daily_ready()
        self.send('只用3D表情')  # cancels today's queued image, does not create a second event
        with self.sessions() as db:
            stickers.queue_daily(db, fixtures.BINDINGS)
        self.assertEqual(self.count(), 1)
        self.now += timedelta(days=1)
        with self.sessions() as db:
            stickers.queue_daily(db, fixtures.BINDINGS)
            row = db.scalar(select(Delivery).where(Delivery.owner_id == self.alice, Delivery.status == 'pending'))
            self.assertIn(row.sticker_id, {'fluent_coffee', 'fluent_wave'})

    def test_successful_create_snooze_complete_react_but_failed_or_clarify_do_not(self):
        first = self.send('两分钟后提醒我写作业').json()
        self.assertEqual(first['action'], 'create')
        self.assertEqual(self.count(), 1)
        changed = self.send('改到明天下午四点', mid='snooze').json()
        self.assertEqual(changed['action'], 'snooze')
        self.assertEqual(self.count(), 2)
        done = self.send('完成写作业', mid='done').json()
        self.assertEqual(done['task']['status'], 'done')
        self.assertEqual(self.count(), 3)
        with self.sessions() as db:
            self.assertIn(db.scalars(select(Delivery).order_by(Delivery.id.desc())).first().sticker_id, SCENES['celebrate'])
        self.send('完成不存在的作业', mid='missing')
        self.send('提醒我洗衣服', mid='clarify')
        self.assertEqual(self.count(), 3)
        self.send('完成写作业', mid='done')
        self.assertEqual(self.count(), 3)

    def test_task_reactions_honor_chat_off_and_serious_context(self):
        self.send('两分钟后提醒我吃药')
        self.assertEqual(self.count(), 0)
        self.send('关闭聊天表情', mid='off')
        self.send('五分钟后提醒我写作业', mid='homework')
        self.assertEqual(self.count(), 0)
        self.send('查询待办', mid='list')
        self.send('打开日程', mid='schedule')
        self.assertEqual(self.count(), 0)
