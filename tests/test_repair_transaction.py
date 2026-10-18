# Isolated transaction test; no QQ client or production profile is used.
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('helper', Path(__file__).resolve().parents[1] / 'services/snowluma_repair_helper.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class TransactionTests(unittest.TestCase):
    def exercise(self, fail_start=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            profile = home / '.config/QQ'
            profile.mkdir(parents=True)
            (profile / 'marker').write_text('original')
            backup_root = root / 'backups'
            calls = []
            def command(*args):
                calls.append(args)
                if args[0] == 'start':
                    profile.mkdir(exist_ok=True)
                    (profile / 'fresh').write_text('new')
                    if fail_start and len(calls) == 1:
                        raise RuntimeError('simulated start failure')
                return ''
            real_stat = h.os.stat
            def fake_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if str(path) == '/':
                    class Device:
                        st_dev = -1
                    return Device()
                return result
            with patch.object(h, 'ROOT', backup_root), \
                 patch.object(h, 'resolve', return_value=(home, profile)), \
                 patch.object(h, 'snapshot', return_value={'qq-test': ['RUNNING', '123'], 'qq-other': ['RUNNING', '456']}), \
                 patch.object(h, 'stop'), patch.object(h, 'supervisor', side_effect=command), \
                 patch.object(h.os, 'stat', side_effect=fake_stat), \
                 patch.object(h.os, 'statvfs', create=True, return_value=type('Space', (), {'f_bavail': 1000000, 'f_frsize': 4096})()):
                if fail_start:
                    with self.assertRaises(RuntimeError):
                        h.begin('qq-test', '123')
                    self.assertTrue((profile / 'marker').exists())
                else:
                    result = h.begin('qq-test', '123')
                    self.assertEqual(Path(result['backup']).joinpath('marker').read_text(), 'original')
                    self.assertTrue((profile / 'fresh').exists())
                    end = h.finish(backup_root / result['transaction'], 'qq-test', '123')
                    self.assertEqual(end['phase'], 'committed')
    def test_real_directory_move_and_commit(self):
        self.exercise()
    def test_real_directory_rollback_after_start_failure(self):
        self.exercise(True)


if __name__ == '__main__':
    unittest.main()
