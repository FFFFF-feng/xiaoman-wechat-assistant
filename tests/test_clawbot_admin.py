import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

import httpx

spec = importlib.util.spec_from_file_location("clawbot_admin", Path(__file__).resolve().parents[1] / "scripts/manage-clawbot.py")
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


class InvitationDisplayTests(TestCase):
    def run_invite(self, show_link):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "bridge.key"; key.write_text("a" * 64)
            output = root / "invitation.json"
            fixture = "fixture-invitation-" + "x" * 43
            def reply(request):
                self.assertEqual(request.url.host, "127.0.0.1")
                self.assertEqual(request.method, "POST")
                return httpx.Response(200, json={"id": 1, "invite": fixture})
            real_client = httpx.Client
            arguments = ["manage-clawbot.py", "invite", "--token-file", str(key), "--label", "测试",
                         "--public-url", "https://schedule.example.com", "--output", str(output)]
            if show_link:
                arguments += ["--show-link"]
            stdout = io.StringIO()
            with patch("sys.argv", arguments), patch.object(admin.httpx, "Client", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw)), contextlib.redirect_stdout(stdout):
                admin.main()
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["url"], "https://schedule.example.com/join#invite=" + fixture)
            self.assertNotIn("a" * 64, stdout.getvalue())
            return fixture, stdout.getvalue()

    def test_default_does_not_display_bearer(self):
        fixture, stdout = self.run_invite(False)
        self.assertNotIn(fixture, stdout)

    def test_explicit_show_link_displays_new_invitation(self):
        fixture, stdout = self.run_invite(True)
        self.assertIn("https://schedule.example.com/join#invite=" + fixture, stdout)

    def test_display_flag_is_rejected_for_other_actions(self):
        with patch("sys.argv", ["manage-clawbot.py", "members", "--token-file", "absent", "--show-link"]), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            admin.main()
        self.assertEqual(raised.exception.code, 2)
