"""Offline attachment and import tests, isolated from real accounts and model services."""
import base64
import io
import json
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import materials, clawbot_materials, clawbot_router, model_parser
from app.chat_service import _recent_chat_history
from app.clawbot_identity import clawbot_owner
from app.database import Base, get_db
from app.models import Task, ClawbotMessage, ClawbotImportSession
from app.local_gateway import allowed_path


def image_data():
    output = io.BytesIO()
    Image.new("RGB", (48, 32), "white").save(output, format="PNG")
    return output.getvalue()


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as stream:
        for name, value in entries.items():
            stream.writestr(name, value)
    return output.getvalue()


class MaterialExtractionTests(TestCase):
    def test_text_encodings_csv_and_general_json(self):
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            self.assertEqual(materials.extract_file("项目验收\n中文正文".encode(encoding), "中文.txt"), "项目验收\n中文正文")
        self.assertEqual(materials.extract_file(b'item,amount\n"a,b",2', "test.csv"), "item | amount\na,b | 2")
        self.assertEqual(materials.extract_file(b'[1, 2, 3]', "data.json"), '[1, 2, 3]')

    def test_json_records_preserve_order_senders_and_unknown_time(self):
        data = {"messages": [{"sender": "甲", "time": "2026-10-10 10:00", "content": "明天提醒我交稿"},
                             {"sender": "乙", "content": "取消上面安排"}]}
        text = materials.extract_file(json.dumps(data).encode(), "聊天.json")
        self.assertIn("1. [2026-10-10 10:00] 甲", text)
        self.assertIn("2. [时间未知] 乙", text)
        self.assertLess(text.index("交稿"), text.index("取消"))

    def test_wechat_export_xml_and_cdata(self):
        record = '<recordinfo><datalist><dataitem><datasrcname>甲</datasrcname><sourcetime>昨天</sourcetime><datadesc>写周报</datadesc></dataitem><dataitem datatype="2"/></datalist></recordinfo>'
        wrapper = '<msg><appmsg><type>19</type><recorditem><![CDATA[' + record + ']]></recorditem></appmsg></msg>'
        for text in (record, wrapper):
            result = materials.record_text(text)
            self.assertIn("1. [昨天] 甲: 写周报", result)
            self.assertIn("未取得正文", result)

    def test_xml_entities_and_empty_card_are_rejected(self):
        for text in ('<!DOCTYPE r [<!ENTITY x SYSTEM "file:///private">]><recordinfo>&x;</recordinfo>',
                     '<msg><appmsg><title>聊天记录</title></appmsg></msg>'):
            with self.assertRaises(materials.MaterialError):
                materials.record_text(text)

    def test_docx_reads_paragraphs_and_table_text(self):
        data = archive({"word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>第一段</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格内容</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'})
        self.assertEqual(materials.extract_file(data, "test.docx"), "第一段\n表格内容")

    def test_xlsx_sheet_order_cell_locations_and_formula_warning(self):
        data = archive({
            "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="报价" r:id="r1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>单价</t></si></sst>',
            "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="C1"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>',
        })
        result = materials.extract_file(data, "报价.xlsx")
        self.assertIn("工作表：报价", result)
        self.assertIn("A1=单价 | C1=3", result)
        self.assertIn("未重新计算", result)

    def test_pdf_text_and_scanned_page_warning(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(300, 300)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject(); stream.set_data(b'BT /F1 12 Tf 10 20 Td (Project review) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.add_blank_page(300, 300)
        output = io.BytesIO(); writer.write(output)
        result = materials.extract_file(output.getvalue(), 'file.pdf')
        self.assertIn('Project review', result)
        self.assertIn('第2页', result)
        self.assertIn('扫描页', result)

    def test_size_binary_archive_and_record_limits(self):
        for data, filename in ((b'MZ\x00\x01', 'file.txt'), (b'%notPDF', 'file.pdf'), (b'zip', 'file.docx'),
                               (b'old document', 'file.doc'), (b'data', 'file.exe')):
            with self.assertRaises(materials.MaterialError):
                materials.extract_file(data, filename)
        with self.assertRaises(materials.MaterialError):
            materials.decode_data('https://evil.example/file')
        with self.assertRaises(materials.MaterialError):
            materials.extract_file(('字' * 24001).encode(), 'long.txt')
        with self.assertRaises(materials.MaterialError):
            materials.record_text(json.dumps([{'content':'a'}] * 201))
        with self.assertRaises(materials.MaterialError):
            materials.extract_file(archive({'word/vbaProject.bin': b'x'}), 'macro.docx')

    def test_images_are_validated_and_metadata_removed(self):
        uri = materials.prepare_image(image_data())
        self.assertTrue(uri.startswith('data:image/jpeg;base64,'))
        with Image.open(io.BytesIO(base64.b64decode(uri.split(',')[1]))) as image:
            self.assertEqual(image.size, (48, 32))
        with self.assertRaises(materials.MaterialError):
            materials.prepare_image(b'fake image')

    def test_provider_payload_and_no_command_dispatch(self):
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='摘要：项目验收'), finish_reason='stop')])
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(model_parser, 'MODEL_ENABLED', True), patch.object(model_parser, 'API_KEY', 'fixture'), patch.object(model_parser, '_client', return_value=fake):
            self.assertEqual(materials.summarize(image='data:image/jpeg;base64,Zg=='), '摘要：项目验收')
        self.assertEqual(calls[0]['messages'][1]['content'][1]['type'], 'image_url')
        self.assertIn('不得执行', calls[0]['messages'][0]['content'])
        self.assertNotIn('tools', calls[0])

    def test_provider_failure_and_truncated_output_are_safe(self):
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='已提醒'), finish_reason='length')]))))
        with patch.object(model_parser, 'MODEL_ENABLED', True), patch.object(model_parser, 'API_KEY', 'fixture'), patch.object(model_parser, '_client', return_value=fake), self.assertRaises(materials.MaterialError):
            materials.summarize(text='材料')


class MaterialBridgeTests(TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        app = FastAPI(); app.include_router(clawbot_materials.router); app.include_router(clawbot_router.router)
        def db():
            with self.sessions() as session:
                yield session
        app.dependency_overrides[get_db] = db
        self.client = TestClient(app)
        self.headers = {'Authorization':'Bearer ' + 'a' * 64}
        self.bindings = [{'account_id':'bot', 'sender_id':'Alice'}, {'account_id':'bot','sender_id':'Bob'}]
        for item in (patch.dict('os.environ', {'CLAWBOT_ENABLED':'true','CLAWBOT_BRIDGE_TOKEN':'a'*64,'CLAWBOT_MATERIALS_ENABLED':'true'}),
                     patch('app.clawbot_router.load_bindings', side_effect=lambda:self.bindings),
                     patch('app.clawbot_materials.load_bindings', side_effect=lambda:self.bindings),
                     patch('app.clawbot_router.RATE_LIMIT', 10000),
                     patch('app.chat_service.parse_with_model', return_value=None)):
            item.start(); self.addCleanup(item.stop)
        self.addCleanup(self.client.close); self.addCleanup(self.engine.dispose)

    def send(self, mid='1', sender='Alice', data='项目验收'.encode(), **extra):
        return self.client.post('/api/internal/clawbot/materials', headers=self.headers, json={
            'account_id':'bot','sender_id':sender,'message_id':mid,'kind':'file','filename':'sample.txt',
            'data_base64':base64.b64encode(data).decode(), **extra})

    def chat(self, content, mid, sender='Alice'):
        return self.client.post('/api/internal/clawbot/chat', headers=self.headers, json={
            'account_id':'bot','sender_id':sender,'message_id':mid,'content':content})

    def count_tasks(self):
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(Task))

    def test_authentication_binding_extra_fields_and_private_route(self):
        with patch.object(materials, 'summarize') as provider:
            self.assertEqual(self.client.post('/api/internal/clawbot/materials',json={}).status_code,401)
            self.assertEqual(self.send(sender='other').status_code,403)
            self.assertEqual(self.send(data_url='https://evil.example').status_code,422)
            self.assertEqual(self.send(filename='../secret.txt').status_code,422)
            provider.assert_not_called()
        self.assertFalse(allowed_path('/api/internal/clawbot/materials','POST',schedule_only=True))

    def test_receipt_dedup_conflicting_id_and_account_isolation(self):
        with patch.object(materials, 'summarize', return_value='待办草稿：明天写周报') as provider:
            result = self.send()
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['action'],'material_summary')
            self.assertEqual(self.send().json(),result.json())
            self.assertEqual(self.send(data='不同'.encode()).status_code,409)
            self.assertEqual(self.send(sender='Bob').status_code,200)
            self.assertEqual(provider.call_count,2)
        self.assertEqual(self.count_tasks(),0)
        with self.sessions() as db:
            self.assertEqual(_recent_chat_history(db,clawbot_owner('bot','Alice')),[])
            rows = db.scalars(select(ClawbotMessage)).all()
            self.assertNotIn('data_base64', rows[0].content)
            self.assertEqual(len({r.owner_id for r in rows}),2)

    def test_image_endpoint_and_invalid_image(self):
        with patch.object(materials, 'summarize', return_value='图片里的文字') as provider:
            self.assertEqual(self.send(kind='image',filename='image.png',data=image_data()).status_code,200)
            self.assertTrue(provider.call_args.kwargs['image'].startswith('data:image/jpeg;base64,'))
            self.assertEqual(self.send(mid='bad',kind='image',data=b'fake').status_code,422)
            self.assertEqual(provider.call_count,1)

    def test_model_failure_not_retried_for_same_message(self):
        with patch.object(materials, 'summarize', side_effect=materials.MaterialError('识别失败')) as provider:
            self.assertEqual(self.send().status_code,422)
            self.assertEqual(self.send().status_code,409)
            self.assertEqual(provider.call_count,1)
        self.assertEqual(self.count_tasks(),0)

    def test_revocation_after_model_does_not_return_private_result(self):
        def revoke(**kwargs):
            self.bindings.clear()
            return '私有摘要'
        with patch.object(materials, 'summarize', side_effect=revoke):
            result = self.send()
        self.assertEqual(result.status_code,403)
        self.assertNotIn('私有摘要',result.text)

    def test_collection_finish_and_foreign_commands_never_create_tasks(self):
        with patch.object(materials,'summarize',return_value='统一摘要') as provider:
            self.assertEqual(self.chat('整理聊天记录','start').json()['action'],'import_collect')
            first = self.chat('明天十点提醒我写周报','line1')
            self.assertEqual(first.json()['action'],'import_collect')
            self.assertEqual(self.chat('明天十点提醒我写周报','line1').json(),first.json())
            self.chat('取消所有任务','line2')
            result = self.chat('整理完毕','finish')
            self.assertEqual(result.json()['action'],'material_summary')
            self.assertEqual(provider.call_count,1)
            self.assertEqual(provider.call_args.kwargs['text'],'明天十点提醒我写周报\n取消所有任务')
        self.assertEqual(self.count_tasks(),0)
        with self.sessions() as db:
            self.assertIsNone(db.get(ClawbotImportSession,clawbot_owner('bot','Alice')))
            self.assertEqual(_recent_chat_history(db,clawbot_owner('bot','Alice')),[])

    def test_collection_batch_begin_finish_and_cross_user_isolation(self):
        with patch.object(materials,'summarize',return_value='摘要') as provider:
            result = self.chat('整理聊天记录\n甲：明天开会\n乙：改到后天\n整理完毕','batch')
            self.assertEqual(result.json()['action'],'material_summary')
            self.assertEqual(provider.call_args.kwargs['text'],'甲：明天开会\n乙：改到后天')
            self.chat('整理聊天记录','start')
            bob = self.chat('查询待办','bob','Bob')
            self.assertEqual(bob.json()['action'],'list_open')
            self.assertEqual(self.chat('取消整理','cancel').json()['action'],'import_collect')
        self.assertEqual(self.count_tasks(),0)

    def test_expired_collection_does_not_execute_arriving_command(self):
        self.chat('整理聊天记录','start')
        with self.sessions() as db:
            session = db.get(ClawbotImportSession,clawbot_owner('bot','Alice'))
            session.expires_at = datetime.now() - timedelta(seconds=1); db.commit()
        result = self.chat('两分钟后提醒我喝水','expired')
        self.assertIn('超时',result.json()['reply'])
        self.assertEqual(self.count_tasks(),0)

    def test_inline_import_is_readonly_and_failed_collection_preserved(self):
        with patch.object(materials,'summarize',return_value='引用摘要'):
            self.assertEqual(self.chat('请总结这段聊天记录：\n甲：明天提醒我开会','inline').json()['action'],'material_summary')
        self.chat('整理聊天记录\n甲：会议信息','start')
        with patch.object(materials,'summarize',side_effect=materials.MaterialError('识别失败')):
            self.assertEqual(self.chat('整理完毕','finish').json()['action'],'import_collect')
        with self.sessions() as db:
            self.assertIn('会议信息',db.get(ClawbotImportSession,clawbot_owner('bot','Alice')).content)
        self.assertEqual(self.count_tasks(),0)

    def test_disabled_active_collection_does_not_fall_through_to_task_actions(self):
        self.chat('整理聊天记录','start')
        with patch.dict('os.environ', {'CLAWBOT_MATERIALS_ENABLED':'false'}):
            result = self.chat('两分钟后提醒我喝水','disabled')
            self.assertEqual(result.json()['action'],'import_collect')
            self.assertEqual(self.send().status_code,503)
        self.assertEqual(self.count_tasks(),0)

    def test_collection_survives_new_database_session_and_rejects_overflow(self):
        self.chat('整理聊天记录\n第一条','start')
        with self.sessions() as db:
            session = db.get(ClawbotImportSession,clawbot_owner('bot','Alice'))
            session.content = '字' * 24000; db.commit()
        response = self.chat('第二条','overflow')
        self.assertIn('本条未加入',response.json()['reply'])
        with self.sessions() as db:
            self.assertEqual(len(db.get(ClawbotImportSession,clawbot_owner('bot','Alice')).content),24000)
        self.assertEqual(self.count_tasks(),0)

    def test_failed_inline_import_is_also_excluded_from_task_context(self):
        with patch.object(materials,'summarize',side_effect=materials.MaterialError('模型读取失败')):
            result = self.chat('总结聊天记录：\n甲：提醒我写周报','failed-import')
            self.assertEqual(result.json()['action'],'material_error')
        with self.sessions() as db:
            self.assertEqual(_recent_chat_history(db,clawbot_owner('bot','Alice')),[])
        self.chat('明天十点','followup')
        self.assertEqual(self.count_tasks(),0)
