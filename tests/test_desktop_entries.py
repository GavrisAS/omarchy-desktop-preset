"""Desktop-entry launches with synthetic XDG data and window inventories."""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_v2 import ROOT, FakeHypr, FakeNautilus, node, preset, v2, window

ID = 'sample-app.desktop'
CLASS = 'com.example.SampleWindow'


@unittest.skipUnless(v2.DesktopEntries().api, 'PyGObject with GIO unavailable')
class GioTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.apps = self.root / 'user/applications'
        self.system = self.root / 'system/applications'
        self.apps.mkdir(parents=True)
        self.system.mkdir(parents=True)
        self.target = self.root / 'Révision One.AppImage'
        self.result = self.root / 'result'
        self.make_executable(self.target, 'one')

    def make_executable(self, path, version):
        path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{version}" "$PWD" "$@" > "$RESULT"\n')
        path.chmod(0o700)

    def entry(self, path=None, target=None, options=None):
        path = path or self.apps / ID
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[Desktop Entry]\nType=Application\nName=ExampleIDE\n'
                        f'Exec="{target or self.target}" %U %u %F %f %% "two words"\n'
                        f'Path={self.root}\nTerminal=false\n' + (options or ''))
        return path

    def run_gio(self, code, fresh_bus=False, **env):
        launcher = ['dbus-run-session', '--'] if fresh_bus else []
        result = subprocess.run(
            [*launcher, sys.executable, '-c', 'from test_desktop_entries import v2, ID, CLASS\n'
             'import os\nfrom pathlib import Path\n' + code],
            env={**os.environ, 'XDG_DATA_HOME': str(self.apps.parent),
                 'XDG_DATA_DIRS': str(self.system.parent), 'RESULT': str(self.result),
                 'PYTHONPATH': str(ROOT / 'tests'), **env},
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8, check=True)
        return result.stdout.strip()

    def wait_for_result(self):
        deadline = time.monotonic() + 3
        while not self.result.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        return self.result.read_text().splitlines()

    def test_mount_process_saves_desktop_identity_and_resolves_updated_exec(self):
        self.entry(options=f'StartupWMClass={CLASS}\n')
        # This running executable and its cwd are intentionally launchable only
        # while the synthetic AppImage mount exists.
        mount = self.root / '.mount_sample123'
        mount.mkdir()
        runner = mount / 'AppRun'
        runner.symlink_to(shutil.which('sleep'))
        proc = subprocess.Popen([runner, '30'], cwd=mount)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        self.run_gio("""
from test_v2 import FakeHypr, FakeNautilus
n = FakeNautilus()
h = FakeHypr(n)
h.windows = [{'class': CLASS, 'initialClass': CLASS, 'pid': int(os.environ['WINDOW_PID']),
              'workspace': {'id': 1}, 'address': '0x100', 'at': [6, 6], 'size': [2548, 1038],
              'floating': False}]
root = Path(os.environ['RESULT']).parent
saved = v2.save(root / 'sample.preset', 1, False, {}, h, v2.Runtime(root / 'rt'), n)
w = saved['windows'][0]
assert w['desktop_id'] == ID and w['argv'] is None and w['cwd'] is None, w
assert '.mount_' not in (root / 'sample.preset').read_text()
""", WINDOW_PID=str(proc.pid))
        self.target.unlink()
        updated = self.root / 'Révision Two.AppImage'
        self.make_executable(updated, 'two')
        self.entry(target=updated, options=f'StartupWMClass={CLASS}\n')
        self.run_gio("""
root = Path(os.environ['RESULT']).parent
w = v2.validate(v2.read_json(root / 'sample.preset'))['windows'][0]
d = v2.DesktopEntries()
d.launch(d.get(w['desktop_id']), 'sample-token')
""")
        self.assertEqual(self.wait_for_result(), ['two', str(self.root), '%', 'two words'])

    def test_class_and_names_are_not_fuzzy_and_wmclass_conflict_is_rejected(self):
        self.entry(options=f'StartupWMClass={CLASS}\n')
        self.entry(self.apps / 'other.desktop', options=f'StartupWMClass={CLASS}\n')
        output = self.run_gio("""
d = v2.DesktopEntries()
assert d.identify({'class': 'ExampleIDE', 'title': 'ExampleIDE'}) is None
try:
    d.identify({'class': CLASS})
except v2.PresetError as exc:
    print(exc)
else:
    raise AssertionError('ambiguous entries associated')
""")
        self.assertIn('ambiguous desktop entries', output)

    def test_gio_metadata_requires_matching_pid_and_precedes_class(self):
        primary = self.entry()
        self.entry(self.apps / (CLASS + '.desktop'))
        self.run_gio("""
from unittest.mock import patch
d = v2.DesktopEntries()
env = {b'GIO_LAUNCHED_DESKTOP_FILE': os.environ['ENTRY'],
       b'GIO_LAUNCHED_DESKTOP_FILE_PID': '42'}
with patch.object(v2, 'process_chain',
                  return_value=[((43, 'start'), env), ((42, 'earlier'), env)]):
    assert d.identify({'class': CLASS, 'pid': 43}) == ID
with patch.object(v2, 'process_chain',
                  return_value=[((43, 'start'), env)]):
    assert d.identify({'class': CLASS, 'pid': 43}) == CLASS + '.desktop'
""", ENTRY=str(primary))

    def test_appimage_metadata_can_match_an_unrelated_id_and_class(self):
        self.entry()
        self.run_gio("""
from unittest.mock import patch
d = v2.DesktopEntries()
env = {b'APPIMAGE': os.environ['IMAGE'], b'APPDIR': '/tmp/.mount_sample123'}
with patch.object(v2, 'process_chain', return_value=[((42, 'start'), env)]):
    with patch.object(os, 'readlink', return_value='/tmp/.mount_sample123/bin/example'):
        assert d.identify({'class': CLASS, 'pid': 42}) == ID
    with patch.object(os, 'readlink', return_value='/home/example/bin/other'):
        assert d.identify({'class': CLASS, 'pid': 42}) is None
""", IMAGE=str(self.target))

    def test_xdg_precedence_nested_ids_and_collision(self):
        self.entry(self.system / 'sample/app.desktop')
        self.entry(options='TryExec=/nonexistent/example\n')
        self.run_gio("""
d = v2.DesktopEntries()
try:
    d.get(ID)
except v2.PresetError as exc:
    assert 'TryExec' in str(exc), exc
else:
    raise AssertionError('unusable user override allowed a different app')
""")
        (self.apps / ID).unlink()
        self.assertTrue(self.run_gio("print(v2.DesktopEntries().get(ID).get_filename())")
                        .endswith('system/applications/sample/app.desktop'))
        self.entry(self.system / ID)
        output = self.run_gio("""
try:
    v2.DesktopEntries().get(ID)
except v2.PresetError as exc:
    print(exc)
""")
        self.assertIn('multiple files', output)

    def test_missing_desktop_file_and_missing_binary(self):
        for missing in ('entry', 'binary'):
            with self.subTest(missing=missing):
                if missing == 'binary':
                    self.entry(target=self.root / 'missing.AppImage')
                output = self.run_gio("""
try:
    v2.DesktopEntries().get(ID)
except v2.PresetError as exc:
    print(exc)
""")
                self.assertIn('missing, hidden or unusable', output)

    def test_gui_process_does_not_hold_cli_output_open(self):
        self.target.write_text('#!/bin/sh\necho "$$" > "$RESULT"\nexec sleep 30\n')
        self.entry()
        try:
            self.run_gio("d = v2.DesktopEntries(); d.launch(d.get(ID), 'sample-token')")
        finally:
            if self.result.exists():
                os.kill(int(self.result.read_text()), 15)

    @unittest.skipUnless(shutil.which('dbus-run-session'), 'dbus-run-session unavailable')
    def test_dbus_activation_uses_service_and_resolves_owner(self):
        name = 'com.example.Sample'
        self.entry(self.apps / f'{name}.desktop', options='DBusActivatable=true\n')
        service = self.root / 'service.py'
        service.write_text(f'''import os
from pathlib import Path
from gi.repository import Gio, GLib
app = Gio.Application(application_id={name!r}, flags=Gio.ApplicationFlags.IS_SERVICE)
def activate(app):
    Path(os.environ['RESULT']).write_text(str(os.getpid()))
    app.hold()
    GLib.timeout_add(500, app.release)
app.connect('activate', activate)
app.run([])
''')
        services = self.apps.parent / 'dbus-1/services'
        services.mkdir(parents=True)
        (services / f'{name}.service').write_text('[D-BUS Service]\n'
                                                f'Name={name}\nExec={sys.executable} {service}\n')
        self.run_gio(f"""
import time
d = v2.DesktopEntries()
app = d.get({name + '.desktop'!r})
d.launch(app, 'sample-token')
result = Path(os.environ['RESULT'])
deadline = time.monotonic() + 3
while not result.exists() and time.monotonic() < deadline:
    time.sleep(.02)
assert d.owner_pid(app) == int(result.read_text())
""", fresh_bus=True)


class DesktopRestoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'sample.preset'
        self.rt = v2.Runtime(self.root / 'runtime')
        self.n = FakeNautilus()
        self.h = FakeHypr(self.n)
        self.engine = v2.Loader(self.h, self.rt, self.n, timeout=.05)
        self.engine.wait = .001
        self.engine.settle = lambda: None
        self.desktop = Mock()
        self.desktop.owner_pid.return_value = None
        self.engine.desktop = self.desktop

    def write(self, *windows, tree=None):
        v2.atomic_json(self.path, preset(list(windows), tree))

    def desktop_window(self):
        return {**window('a', CLASS), 'argv': None, 'desktop_id': ID}

    def client(self, address, pid, ws):
        return {'class': CLASS, 'pid': pid, 'address': address,
                'workspace': {'id': ws, 'name': str(ws)}, 'title': 'identical',
                'at': [6, 6], 'size': [2548, 1038], 'floating': False}

    def test_forking_launch_ignores_same_class_and_reloads_one_window(self):
        self.write(self.desktop_window(), tree={'id': 'a'})
        self.h.windows = [self.client('0x10', 10, 3)]
        metadata = {}

        def spawn(_app, token):
            self.h.pending = [[1, self.client('0x11', 11, 3)],
                              [2, self.client('0x12', 12, 5)]]
            metadata[12] = token
            return set()  # Launcher already exited, window process survives.

        def chain(pid, _keys):
            return iter([((pid, 'start'), {b'DESKTOP_PRESET_LAUNCH_TOKEN': metadata.get(pid)})])

        self.desktop.launch.side_effect = spawn
        with patch.object(v2, 'process_chain', side_effect=chain):
            self.engine.load(self.path, 5)
            self.engine.load(self.path, 5)
        self.assertEqual([self.h.get(a)['workspace']['id'] for a in ('0x10', '0x11', '0x12')],
                         [3, 3, 5])
        self.desktop.launch.assert_called_once()

    def test_dbus_owner_can_create_new_window_but_existing_address_is_excluded(self):
        self.write(self.desktop_window(), tree={'id': 'a'})
        self.h.windows = [self.client('0x10', 10, 3)]
        self.desktop.owner_pid.return_value = 10

        def activate(_app, _token):
            self.h.pending = [[1, self.client('0x11', 10, 5)]]
            return set()

        self.desktop.launch.side_effect = activate
        with patch.object(v2, 'process_chain', side_effect=lambda pid, _: iter([((pid, 'start'), {})])):
            self.engine.load(self.path, 5)
        self.assertEqual(self.h.get('0x10')['workspace']['id'], 3)
        self.assertEqual(self.h.get('0x11')['workspace']['id'], 5)

    def test_occupied_same_class_requires_a_journal_binding(self):
        self.write(self.desktop_window(), tree={'id': 'a'})
        self.h.windows.append(self.client('0x10', 10, 5))
        with self.assertRaisesRegex(v2.PresetError, 'unassigned windows'):
            self.engine.load(self.path, 5)
        self.assertFalse(self.h.dispatches)
        self.desktop.launch.assert_not_called()

    def test_single_instance_without_new_matching_window_leaves_pending(self):
        self.write(self.desktop_window(), tree={'id': 'a'})
        self.h.windows.append(self.client('0x10', 10, 3))
        self.desktop.launch.return_value = set()
        with self.assertRaisesRegex(v2.PresetError, 'timed out'):
            self.engine.load(self.path, 5)
        with self.assertRaisesRegex(v2.PresetError, 'previous launch is unresolved'):
            self.engine.load(self.path, 5)
        self.desktop.launch.assert_called_once()
        self.assertEqual(self.h.get('0x10')['workspace']['id'], 3)

    def test_missing_entry_does_not_use_stale_argv_or_touch_workspace(self):
        w = {**self.desktop_window(), 'argv': ['/example/old.AppImage']}
        self.write(w, tree={'id': 'a'})
        self.desktop.get.side_effect = v2.PresetError('missing desktop entry')
        for options in ({}, {'dry': True}, {'clear': True, 'yes': True}):
            with self.assertRaisesRegex(v2.PresetError, 'launch set'):
                self.engine.load(self.path, 5, **options)
        self.desktop.launch.assert_not_called()
        self.assertFalse(self.h.dispatches)

    def test_targeted_migration_preserves_other_windows_and_journal(self):
        path_old = '/tmp/.mount_sample123/AppRun'
        a = {**window('a', CLASS), 'argv': [path_old], 'cwd': '/tmp/.mount_sample123'}
        b = window('b', v2.NAUTILUS, 'file:///example/independent')
        c = {**window('c', 'com.example.Other', floating=True), 'argv': ['/example/other', 'argument']}
        self.write(a, b, c, tree=node('x', .43, 'a', 'b'))
        mapping = self.root / 'autostart.conf'
        mapping.write_bytes(b'5=sample\n')
        before = self.path.read_bytes()
        state = {'instances': {'a': {'pending': True}, 'b': {'address': '0x10'}}}
        self.rt.data['loads'][self.rt.key(self.path, 5)] = copy.deepcopy(state)
        with patch.object(v2, 'DesktopEntries', return_value=self.desktop):
            v2.set_desktop_launch(self.path, 'a', ID, self.rt)
        expected = v2.validate(json.loads(before))
        expected['windows'][0].update(desktop_id=ID, argv=None, cwd=None)
        self.assertEqual(v2.read_json(self.path), expected)
        self.assertEqual(next(self.root.glob('sample.preset.bak.*')).read_bytes(), before)
        self.assertEqual(mapping.read_bytes(), b'5=sample\n')
        self.assertEqual(self.rt.data['loads'][self.rt.key(self.path, 5)], state)

    def test_special_wrappers_and_direct_argv(self):
        self.assertEqual(v2.launch_info({'class': 'chrome-example.org__-Default'}, self.desktop)['argv'],
                         ['omarchy-launch-webapp', 'https://example.org/'])
        self.assertEqual(v2.launch_info({'class': 'org.omarchy.example'}, self.desktop)['argv'],
                         ['omarchy-launch-tui', 'example'])
        self.assertEqual(v2.launch_info({'class': v2.NAUTILUS}, self.desktop)['argv'],
                         ['nautilus', '--new-window'])
        self.desktop.identify.assert_not_called()
        self.desktop.identify.return_value = None
        with subprocess.Popen([shutil.which('sleep'), '30'], cwd=self.root) as proc:
            try:
                self.assertEqual(v2.launch_info({'class': CLASS, 'pid': proc.pid}, self.desktop),
                                 {'argv': [shutil.which('sleep'), '30'], 'cwd': str(self.root)})
            finally:
                proc.terminate()
