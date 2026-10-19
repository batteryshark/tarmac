"""Offline installer contracts; all service operations mocked."""
import contextlib
import io
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tarmac import service, store


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home & <雪 space'
        self.home.mkdir()
        self.plist = self.home / 'Library/LaunchAgents/local.tarmac.plist'
        self.exe = self.home / 'bin & <雪 space/tarmac'
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'USER': 'demo & <雪',
                    'TARMAC_HOME': str(self.home / 'state & <雪 space'),
                    'EXAMPLE_PROVIDER_KEY': 'synthetic-not-a-credential',
                    'UNRELATED_SETTING': 'must-not-copy'}
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.enterContext(patch.object(service, 'PLIST', self.plist))
        self.enterContext(patch.object(service.shutil, 'which', return_value=str(self.exe)))
        self.run_mock = self.enterContext(patch.object(service.subprocess, 'run'))
        self.run_mock.return_value = subprocess.CompletedProcess([], 0, '', '')
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))

    def install(self):
        self.assertEqual(service.run('install'), 0)
        return plistlib.loads(self.plist.read_bytes())

    def test_fresh_parent_roundtrip_and_startup_environment(self):
        self.assertFalse(self.plist.parent.exists())
        data = self.install()
        env = data['EnvironmentVariables']
        self.assertEqual(set(env), {'HOME', 'PATH', 'USER', 'LOGNAME', 'TARMAC_HOME'})
        self.assertEqual(env['HOME'], str(self.home))
        self.assertEqual(env['USER'], self.env['USER'])
        self.assertEqual(env['LOGNAME'], self.env['USER'])
        with patch.dict(os.environ, env, clear=True):
            state = store.home()
            self.assertEqual(state, Path(self.env['TARMAC_HOME']).resolve())
            self.assertEqual(data['StandardOutPath'], str(state / 'daemon.out.log'))
            self.assertEqual(data['StandardErrorPath'], str(state / 'daemon.err.log'))
        self.assertEqual(data['ProgramArguments'], [str(self.exe), 'daemon'])
        self.assertTrue(data['RunAtLoad'])
        self.assertTrue(data['KeepAlive'])
        self.assertEqual(self.run_mock.call_args_list[0].args[0][1], 'bootout')
        self.assertEqual(self.run_mock.call_args_list[1].args[0],
                         ['launchctl', 'bootstrap', service._domain(), str(self.plist)])

    def test_defaults_and_tilde_are_pinned(self):
        os.environ.pop('TARMAC_HOME')
        self.assertEqual(self.install()['EnvironmentVariables']['TARMAC_HOME'],
                         str(self.home / '.tarmac'))
        os.environ['TARMAC_HOME'] = '~/custom & <雪'
        self.assertEqual(self.install()['EnvironmentVariables']['TARMAC_HOME'],
                         str(self.home / 'custom & <雪'))

    def test_invalid_state_fails_before_service_calls(self):
        for value in ('', 'relative'):
            with self.subTest(value=value), patch.dict(os.environ, {'TARMAC_HOME': value}):
                self.assertEqual(service.run('install'), 1)
        self.run_mock.assert_not_called()
        self.assertFalse(self.plist.exists())

    def test_bootstrap_failure_bounded_diagnostics(self):
        for stderr, stdout, expected in [('mock failure' * 1000, '', 'mock failure'),
                                         ('', 'mock stdout', 'mock stdout'), ('', '', 'no diagnostic')]:
            with self.subTest(expected=expected):
                self.output.seek(0)
                self.output.truncate()
                self.run_mock.side_effect = [subprocess.CompletedProcess([], 1, '', ''),
                                            subprocess.CompletedProcess([], 5, stdout, stderr)]
                self.assertEqual(service.run('install'), 5)
                text = self.output.getvalue()
                self.assertIn('bootstrap failed (5)', text)
                self.assertIn(expected, text)
                self.assertNotIn('installed', text)
                self.assertLess(len(text), 600)
                self.assertTrue(self.plist.exists())

    def test_unrepresentable_environment_fails_without_service_calls(self):
        os.environ['USER'] = 'invalid\x01xml'
        self.assertEqual(service.run('install'), 1)
        self.run_mock.assert_not_called()
        self.assertFalse(self.plist.exists())

    def test_resolved_state_keeps_existing_contents_in_place(self):
        state = self.home / 'actual state & <雪'
        state.mkdir()
        sentinel = state / 'existing-state'
        sentinel.write_bytes(b'unchanged state fixture')
        alias = self.home / 'state alias'
        alias.symlink_to(state, target_is_directory=True)
        os.environ['TARMAC_HOME'] = str(alias)
        data = self.install()
        self.assertEqual(data['EnvironmentVariables']['TARMAC_HOME'], str(state))
        self.assertEqual(data['StandardOutPath'], str(state / 'daemon.out.log'))
        self.assertEqual(sentinel.read_bytes(), b'unchanged state fixture')
        self.assertEqual(list(state.iterdir()), [sentinel])
