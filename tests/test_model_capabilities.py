from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from app import model_parser


class ModelCapabilityTests(TestCase):
    def test_chat_receives_current_image_capability_instead_of_inventing_text_only(self):
        fake=Mock()
        fake.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"action":"chat","title":null,"due_at":null,"confidence":"high","message":"可以用图片表情。"}'))])
        with patch.object(model_parser,'MODEL_ENABLED',True),patch.object(model_parser,'API_KEY','synthetic'),patch.object(model_parser,'_client',return_value=fake),patch.dict('os.environ',{'CLAWBOT_ENABLED':'true','CLAWBOT_STICKERS_ENABLED':'true'}):
            response=model_parser.parse_with_model('你能发表情图片吗')
        self.assertEqual(response.action,'chat')
        prompt=fake.chat.completions.create.call_args.kwargs['messages'][0]['content']
        self.assertIn('当前微信插件支持独立发送小满图片表情',prompt)
        self.assertIn('不要声称图片已经送达',prompt)
        self.assertIn('红色小凤鸟',prompt)
