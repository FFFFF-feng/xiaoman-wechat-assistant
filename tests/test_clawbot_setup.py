import importlib.util
import json
from pathlib import Path
import tempfile
from unittest import TestCase

spec = importlib.util.spec_from_file_location("clawbot_setup", Path(__file__).resolve().parents[1] / "scripts/setup-clawbot.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class RuntimeSetupTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "new-runtime"

    def initialize(self):
        return setup.init_runtime(self.root, "https://schedule.example.com")

    def install_fixture(self, version="2.4.10"):
        host = self.root / "npm/node_modules/openclaw"
        native = self.root / "state/npm/projects/plugin-fixture/node_modules/@tencent-weixin/openclaw-weixin"
        host.mkdir(parents=True); native.mkdir(parents=True)
        (host / "package.json").write_text(json.dumps({"version": "2026.10.1"}), encoding="utf-8")
        (native / "package.json").write_text(json.dumps({"name": "@tencent-weixin/openclaw-weixin", "version": version}), encoding="utf-8")

    def test_init_generates_private_isolated_baseline(self):
        self.initialize()
        config = json.loads((self.root / "state/openclaw.json").read_text(encoding="utf-8"))
        self.assertEqual(config["tools"]["deny"], ["*"])
        self.assertFalse(config["gateway"]["controlUi"]["enabled"])
        self.assertEqual(config["session"]["dmScope"], "per-account-channel-peer")
        self.assertEqual(config["agents"]["defaults"]["model"]["fallbacks"], [])
        self.assertEqual(config["agents"]["entries"]["main"]["identity"]["name"], "小满")
        self.assertNotIn("default", config["agents"]["entries"]["main"])
        key = (self.root / "state/xiaoman/bridge.key").read_text().strip()
        self.assertEqual(len(key), 64)
        self.assertIn(key, (self.root / "state/xiaoman/runtime.env").read_text(encoding="utf-8"))
        self.assertNotEqual(key, config["gateway"]["auth"]["token"])
        self.assertEqual(json.loads((self.root / "state/xiaoman/bindings.json").read_text()), [])

    def test_init_does_not_overwrite_existing_runtime(self):
        self.initialize()
        before = (self.root / "state/openclaw.json").read_bytes()
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertEqual(before, (self.root / "state/openclaw.json").read_bytes())

    def test_nonempty_unowned_directory_is_untouched(self):
        self.root.mkdir(); (self.root / "keep.txt").write_text("keep")
        with self.assertRaises(ValueError):
            self.initialize()
        self.assertEqual([p.name for p in self.root.iterdir()], ["keep.txt"])

    def test_invalid_public_address_does_not_create_files(self):
        for url in ("http://schedule.example.com", "https://user:pass@example.com", "https://example.com/path", "https://example.com#secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                setup.init_runtime(self.root, url)
            self.assertFalse(self.root.exists())

    def test_activation_preserves_bindings_and_second_activation_is_noop(self):
        self.initialize(); self.install_fixture()
        bindings = self.root / "state/xiaoman/bindings.json"
        bindings.write_text('[{"account_id":"fixture-bot","sender_id":"fixture-user"}]')
        before = bindings.read_bytes()
        self.assertTrue(setup.activate_runtime(self.root))
        self.assertEqual(before, bindings.read_bytes())
        config_path = self.root / "state/openclaw.json"
        first = config_path.read_bytes()
        config = json.loads(first)
        self.assertTrue(config["plugins"]["entries"]["xiaoman-bridge"]["hooks"]["allowConversationAccess"])
        self.assertFalse(setup.activate_runtime(self.root))
        self.assertEqual(first, config_path.read_bytes())

    def test_wrong_native_version_fails_without_config_write(self):
        self.initialize(); self.install_fixture("2.0.0")
        config_path = self.root / "state/openclaw.json"
        before = config_path.read_bytes()
        with self.assertRaises(ValueError):
            setup.activate_runtime(self.root)
        self.assertEqual(before, config_path.read_bytes())
