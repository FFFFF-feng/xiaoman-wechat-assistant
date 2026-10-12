"""Offline speech/intent checks: fake WAV, fake ASR, isolated SQLite; no platform calls."""
import base64
import io
import json
import math
import struct
import wave
from unittest import TestCase
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import speech, clawbot_voice, clawbot_router
from app.clawbot_identity import clawbot_owner
from app.database import Base, get_db
from app.local_gateway import allowed_path
from app.models import ClawbotVoiceMessage, Task


def wav_data(seconds=0.2, silent=False):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
        stream.writeframes(b''.join(struct.pack('<h', 0 if silent else int(2000 * math.sin(i / 10)))
                                   for i in range(int(seconds * 16000))))
    return buffer.getvalue()


ENV = {'ASR_ENABLED':'true', 'ASR_API_KEY':'fixture-bailian-key', 'ASR_MODEL':'qwen3-asr-flash',
       'ASR_BASE_URL':speech.DEFAULT_BASE}


class SpeechTests(TestCase):
    def test_valid_wav_and_silence(self):
        data = wav_data()
        self.assertEqual(speech.validate_audio(base64.b64encode(data).decode()), data)
        with self.assertRaises(speech.SpeechError) as result:
            speech.validate_audio(base64.b64encode(wav_data(silent=True)).decode())
        self.assertEqual(result.exception.code, 'speech_silent')

    def test_invalid_or_truncated_audio(self):
        for data in (b'not-a-wave', wav_data()[:-4]):
            with self.assertRaises(speech.SpeechError):
                speech.validate_audio(base64.b64encode(data).decode())
        with self.assertRaises(speech.SpeechError):
            speech.validate_audio('https://evil.example/audio.wav')

    def test_configuration_is_opt_in_and_vendor_restricted(self):
        for values in ({**ENV, 'ASR_ENABLED':'false'}, {**ENV, 'ASR_BASE_URL':'https://evil.example/v1'}):
            with patch.dict('os.environ', values), self.assertRaises(speech.SpeechError):
                speech.configuration()
        with patch.dict('os.environ', {**ENV, 'ASR_API_KEY':'', 'DASHSCOPE_API_KEY':'', 'ALIYUN_API_KEY':'',
                                      'MODEL_API_KEY':'other-vendor-key', 'MODEL_BASE_URL':'https://example.com/v1'}), self.assertRaises(speech.SpeechError):
            speech.configuration()

    def test_provider_payload_and_transcript(self):
        def reply(request):
            self.assertEqual(str(request.url), speech.DEFAULT_BASE + '/chat/completions')
            body = json.loads(request.content)
            self.assertEqual(body['model'], 'qwen3-asr-flash')
            self.assertEqual(body['asr_options'], {'enable_itn':False})
            self.assertTrue(body['messages'][0]['content'][0]['input_audio']['data'].startswith('data:audio/wav;base64,'))
            return httpx.Response(200, json={'choices':[{'message':{'content':'两分钟后提醒我喝水'}}]})
        client = httpx.Client
        with patch.dict('os.environ', ENV), patch.object(speech.httpx, 'Client', side_effect=lambda **kw: client(transport=httpx.MockTransport(reply), **kw)):
            self.assertEqual(speech.transcribe(wav_data()), '两分钟后提醒我喝水')

    def test_provider_failure_or_empty_result_never_produces_intent(self):
        client = httpx.Client
        for status, body in ((401, {}), (200, {'choices':[{'message':{'content':''}}]}), (200, {})):
            with patch.dict('os.environ', ENV), patch.object(speech.httpx, 'Client', side_effect=lambda **kw: client(
                    transport=httpx.MockTransport(lambda request:httpx.Response(status, json=body)), **kw)), self.assertRaises(speech.SpeechError):
                speech.transcribe(wav_data())


class VoiceBridgeTests(TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        app = FastAPI(); app.include_router(clawbot_voice.router)
        def database():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = database
        self.client = TestClient(app)
        self.bindings = [{'account_id':'bot', 'sender_id':'Alice'}, {'account_id':'bot', 'sender_id':'Bob'}]
        patches = [patch.dict('os.environ', {**ENV, 'CLAWBOT_ENABLED':'true', 'CLAWBOT_BRIDGE_TOKEN':'a'*64,
                                             'CLAWBOT_REMINDERS_ENABLED':'false', 'PUBLIC_BASE_URL':'', 'WECHAT_ENABLED':'false'}),
                   patch('app.clawbot_voice.load_bindings', side_effect=lambda:self.bindings),
                   patch('app.chat_service.parse_with_model', return_value=None),
                   patch.dict(clawbot_voice._rate_hits, {}, clear=True), patch.dict(clawbot_router._rate_hits, {}, clear=True)]
        for item in patches:
            item.start(); self.addCleanup(item.stop)
        self.addCleanup(self.client.close); self.addCleanup(self.engine.dispose)

    def send(self, sender='Alice', mid='1', data=None, **extra):
        return self.client.post('/api/internal/clawbot/voice', headers={'Authorization':'Bearer '+'a'*64}, json={
            'account_id':'bot', 'sender_id':sender, 'message_id':mid,
            'audio_base64':base64.b64encode(data or wav_data()).decode(), **extra})

    def count_tasks(self):
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(Task))

    def test_identity_authentication_and_public_gateway(self):
        with patch('app.speech.transcribe') as provider:
            self.assertEqual(self.client.post('/api/internal/clawbot/voice', json={}).status_code, 401)
            self.assertEqual(self.send(sender='unbound').status_code, 403)
            self.assertEqual(self.send(audio_url='https://evil.example/a.wav').status_code, 422)
            provider.assert_not_called()
        self.assertFalse(allowed_path('/api/internal/clawbot/voice', 'POST', schedule_only=True))

    def test_task_intent_and_durable_voice_deduplication(self):
        with patch('app.speech.transcribe', return_value='两分钟后提醒我喝水') as provider:
            first = self.send()
            self.assertEqual(first.status_code, 200, first.text)
            self.assertIn('语音识别：', first.json()['reply'])
            self.assertEqual(self.send().json(), first.json())
            self.assertEqual(self.send(data=wav_data(0.3)).status_code, 409)
            self.assertEqual(provider.call_count, 1)
        self.assertEqual(self.count_tasks(), 1)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(Task)).owner_id, clawbot_owner('bot','Alice'))
            row = db.scalar(select(ClawbotVoiceMessage))
            self.assertEqual(row.status, 'processed')
            self.assertEqual(row.transcript, '两分钟后提醒我喝水')

    def test_negative_and_conditional_intents_do_not_create_tasks(self):
        for mid, text in enumerate(('不要提醒我明天十点写周报', '如果明天下雨，下午五点提醒我带伞')):
            with patch('app.speech.transcribe', return_value=text):
                self.assertEqual(self.send(mid=str(mid)).status_code, 200)
        self.assertEqual(self.count_tasks(), 0)

    def test_readonly_homophone_correction_preserves_original_transcript(self):
        with patch('app.speech.transcribe', return_value='查询代办。'):
            result = self.send()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['action'], 'list_open')
        self.assertIn('语音识别：查询代办。', result.json()['reply'])
        self.assertEqual(speech.intent_text('明天十点提醒我办理代办业务'), '明天十点提醒我办理代办业务')
        self.assertEqual(self.count_tasks(), 0)

    def test_failed_asr_is_not_automatically_retried(self):
        with patch('app.speech.transcribe', side_effect=speech.SpeechError('speech_unavailable')) as provider:
            self.assertEqual(self.send().status_code, 422)
            self.assertEqual(self.send().status_code, 409)
            self.assertEqual(provider.call_count, 1)
        self.assertEqual(self.count_tasks(), 0)

    def test_disabled_and_silent_audio_do_not_call_provider(self):
        with patch('app.speech.transcribe') as provider:
            with patch.dict('os.environ', {'ASR_ENABLED':'false'}):
                self.assertEqual(self.send().status_code, 503)
            self.assertEqual(self.send(data=wav_data(silent=True)).status_code, 422)
            provider.assert_not_called()

    def test_revocation_during_asr_blocks_task_execution(self):
        def recognize(data):
            self.bindings = []
            return '两分钟后提醒我喝水'
        with patch('app.speech.transcribe', side_effect=recognize):
            self.assertEqual(self.send().status_code, 403)
        self.assertEqual(self.count_tasks(), 0)

    def test_same_voice_message_id_is_isolated_between_users(self):
        with patch('app.speech.transcribe', side_effect=['两分钟后提醒我甲事项', '两分钟后提醒我乙事项']):
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send(sender='Bob').status_code, 200)
        with self.sessions() as db:
            tasks = db.scalars(select(Task)).all()
            self.assertEqual(len(tasks), 2)
            self.assertNotEqual(tasks[0].owner_id, tasks[1].owner_id)
