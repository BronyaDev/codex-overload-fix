import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure_codex', ROOT / 'scripts/configure_codex.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'out' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'profile with spaces' / 'config.toml'
        self.path.parent.mkdir()

    def write(self, text):
        self.path.write_bytes(text.encode('utf-8'))


class ConfigurationTests(ConfigFixture):
    def test_install_remove_preserves_original_bytes_and_is_idempotent(self):
        originals = [
            b'',
            b'model = "gpt-example"',
            b'model_provider = "other" # keep\r\n[projects."D:/work"]\r\ntrust_level = "trusted"\r\n',
            b'\xef\xbb\xbf# utf8 bom\r\nmodel = "example"\r\n',
            b"'model_provider' = 'other'\n[model_providers.'openai-retry']\nname = 'custom'\n[features]\napps = true\n",
        ]
        for original in originals:
            with self.subTest(original=original):
                self.path.write_bytes(original)
                config.apply('install', self.path)
                installed = self.path.read_bytes()
                self.assertIn(b'http://127.0.0.1:8787', installed)
                config.apply('install', self.path)
                self.assertEqual(installed, self.path.read_bytes())
                config.apply('remove', self.path)
                self.assertEqual(original, self.path.read_bytes())
                config.apply('remove', self.path)
                self.assertEqual(original, self.path.read_bytes())

    def test_preserves_later_unrelated_edits_and_provider_selection(self):
        self.write('model_provider = "before"\nmodel = "first"\n')
        config.apply('install', self.path)
        updated = self.path.read_text().replace('model = "first"', 'model = "later"')
        updated = updated.replace(config.ROOT, 'model_provider = "user-choice"')
        self.write(updated + '\n[features]\napps = true\n')
        config.apply('remove', self.path)
        result = self.path.read_text()
        self.assertIn('model_provider = "user-choice"', result)
        self.assertIn('model = "later"', result)
        self.assertIn('[features]', result)
        self.assertNotIn('[model_providers.openai-retry]', result)

    def test_multiline_strings_arrays_and_quoted_headers_are_not_rewritten(self):
        original = '''notes = """
model_provider = "fake"
[model_providers.openai-retry]
"""
values = [
  "[model_providers.openai-retry]", # array content
]
[projects.'contains # [brackets]']
description = ''' + "'''\nmodel_provider = 'also fake'\n'''\n"
        self.write(original)
        config.apply('install', self.path)
        if config.tomllib:
            parsed = config.tomllib.loads(self.path.read_text())
            self.assertEqual(parsed['model_provider'], config.PROVIDER)
            self.assertIn('fake', parsed['notes'])
        config.apply('remove', self.path)
        self.assertEqual(original, self.path.read_text())

    def test_edited_relay_table_is_not_silently_deleted_and_lock_is_released(self):
        config.apply('install', self.path)
        self.write(self.path.read_text().replace('8787', '9999'))
        before = self.path.read_bytes()
        for action in ('remove', 'check', 'install'):
            with self.assertRaises(ValueError):
                config.apply(action, self.path)
            self.assertEqual(before, self.path.read_bytes())
            self.assertFalse(self.path.with_name('config.toml.overload-fix.lock').exists())

    def test_refuses_inline_or_nested_provider_config(self):
        for original in ('model_providers.openai-retry = { name = "mine" }\n',
                         '[model_providers.openai-retry.headers]\nx = "y"\n'):
            self.write(original)
            with self.assertRaises(ValueError):
                config.apply('install', self.path)
            self.assertEqual(original, self.path.read_text())

    def test_exact_manual_snippet_can_be_removed_without_saved_state(self):
        self.write(config.ROOT + '\nmodel = "keep"\n\n' + config.BLOCK)
        config.apply('remove', self.path)
        self.assertIn('model = "keep"', self.path.read_text())
        self.assertNotIn(config.PROVIDER, self.path.read_text())

    def test_exclusive_lock_prevents_concurrent_write(self):
        lock = self.path.with_name('config.toml.overload-fix.lock')
        lock.touch()
        with self.assertRaises(FileExistsError):
            config.apply('install', self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(lock.exists())


@unittest.skipUnless(os.name == 'nt', 'Windows command launchers')
class WindowsLauncherTests(ConfigFixture):
    def setUp(self):
        super().setUp()
        self.repo = Path(self.temp.name) / "repo space ' 中文"
        (self.repo / 'scripts').mkdir(parents=True)
        for name in ('install.cmd', 'remove-config.cmd', 'uninstall.cmd'):
            shutil.copyfile(ROOT / name, self.repo / name)
        for name in ('configure_codex.py', 'windows-setup.ps1'):
            shutil.copyfile(ROOT / 'scripts' / name, self.repo / 'scripts' / name)
        self.env = os.environ.copy()
        self.env['CODEX_HOME'] = str(self.path.parent)
        self.env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + self.env['PATH']
        self.marker = self.repo / 'default-installer-called'
        self.removed = self.repo / 'default-uninstaller-called'
        self.stub('install.ps1', self.marker)
        self.stub('uninstall.ps1', self.removed)

    def stub(self, name, marker, status=0):
        literal = str(marker).replace("'", "''")
        (self.repo / 'scripts' / name).write_text(
            f"if ($args.Count) {{ exit 99 }}\nSet-Content -LiteralPath '{literal}' -Value called\nexit {status}\n",
            encoding='utf-8-sig')

    def run_cmd(self, name):
        return subprocess.run(f'cmd.exe /d /s /c ""{self.repo / name}""', env=self.env,
                              cwd=ROOT, input='\n', text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', timeout=30)

    def test_actual_cmd_install_remove_and_uninstall(self):
        self.write('model_provider = "original"\nmodel = "keep"\n')
        original = self.path.read_bytes()
        result = self.run_cmd('install.cmd')
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertTrue(self.marker.exists())
        self.assertIn(config.ROOT, self.path.read_text())
        result = self.run_cmd('remove-config.cmd')
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual(original, self.path.read_bytes())
        self.assertFalse(self.removed.exists())
        self.assertEqual(0, self.run_cmd('install.cmd').returncode)
        result = self.run_cmd('uninstall.cmd')
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertTrue(self.removed.exists())
        self.assertEqual(original, self.path.read_bytes())

    def test_failed_default_install_does_not_change_configuration(self):
        self.write('model = "keep"\n')
        original = self.path.read_bytes()
        self.stub('install.ps1', self.marker, 17)
        result = self.run_cmd('install.cmd')
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual(original, self.path.read_bytes())

    def test_refused_config_removal_does_not_stop_relay(self):
        config.apply('install', self.path)
        self.write(self.path.read_text().replace('8787', '9999'))
        result = self.run_cmd('uninstall.cmd')
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertFalse(self.removed.exists())

    def test_configuration_preflight_failure_does_not_run_installer(self):
        self.write('[model_providers.openai-retry.headers]\nx = "keep"\n')
        original = self.path.read_bytes()
        result = self.run_cmd('install.cmd')
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertFalse(self.marker.exists())
        self.assertEqual(original, self.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
