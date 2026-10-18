"""Regression tests; no Docker or real QQ accounts required."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services import snowluma_repair as repair

spec = importlib.util.spec_from_file_location('repair_helper', ROOT / 'services/snowluma_repair_helper.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class RepairTests(unittest.TestCase):
    def test_unknown_program_rejected_before_loading_extractor(self):
        with patch('runpy.run_path', return_value={'ACCOUNTS': {}}):
            for program in ('../qq', 'snowluma', 'qq;stop'):
                with self.assertRaises(ValueError):
                    helper.resolve(program, '123')

    def test_wrong_home_rejected(self):
        with patch('runpy.run_path', return_value={'ACCOUNTS': {'qq-test': ('qq-456', '/app/qq-456')}}):
            with self.assertRaises(ValueError):
                helper.resolve('qq-test', '123')

    def test_lock_reentrant(self):
        with repair.account_lock('test', 'qq-test'):
            with repair.account_lock('test', 'qq-test'):
                pass

    def test_conflicting_thread_rejected(self):
        import threading
        errors = []
        def conflicting():
            try:
                with repair.account_lock('test', 'qq-test'):
                    pass
            except RuntimeError:
                errors.append(True)
        with repair.account_lock('test', 'qq-test'):
            t = threading.Thread(target=conflicting)
            t.start()
            t.join()
        self.assertEqual(errors, [True])

    def test_transaction_rejects_traversal(self):
        with self.assertRaises(ValueError):
            helper.load(Path('/tmp/not-a-transaction'), 'qq-test', '123')

    def test_rollback_preserves_failed_profile_and_restores_original(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            transaction = root / 'transaction'
            transaction.mkdir()
            profile = root / 'QQ'
            profile.mkdir()
            (profile / 'new').write_text('failed')
            original = transaction / 'original'
            original.mkdir()
            (original / 'marker').write_text('original')
            data = {'phase': 'verifying'}
            with patch.object(helper, 'load', return_value=(data, root, profile)), \
                 patch.object(helper, 'stop'), \
                 patch.object(helper, 'snapshot', return_value={'qq-test': ['STOPPED', '']}), \
                 patch.object(helper, 'supervisor') as command:
                result = helper.rollback(transaction, 'qq-test', '123')
            self.assertEqual((profile / 'marker').read_text(), 'original')
            self.assertTrue(list(transaction.glob('failed-*/new')))
            self.assertEqual(result['phase'], 'rolled_back')
            command.assert_called_once_with('start', 'qq-test')

    def test_committed_backup_cannot_be_rolled_back_implicitly(self):
        with patch.object(helper, 'load', return_value=({'phase': 'committed'}, Path('/tmp'), Path('/tmp/QQ'))):
            with self.assertRaises(RuntimeError):
                helper.rollback(Path('/tmp/transaction'), 'qq-test', '123')

    def test_running_account_is_not_reset(self):
        import types
        fake = types.ModuleType('services.snowluma')
        fake.accounts = lambda: {'test': {'container': 'shared', 'program': 'qq-test', 'uin': '123'}}
        fake._container_client = lambda _: object()
        fake._ensure_extractor = lambda _: None
        fake._states_cache = {}
        fake._win_cache = {'ts': 0}
        fake._account_login_stage = lambda _: 'logged_in'
        state = types.ModuleType('services.snowluma_state')
        state._cache = {}
        logger = types.ModuleType('services.operation_logger')
        logger.operation_logger = types.SimpleNamespace(info=lambda *a: None)
        job = {'name': 'test'}
        with patch.dict(sys.modules, {'services.snowluma': fake, 'services.snowluma_state': state,
                                     'services.operation_logger': logger}), \
             patch.object(repair, '_helper') as helper_call:
            repair._run(job)
        self.assertEqual(job['phase'], 'failed')
        helper_call.assert_not_called()

    def test_working_qr_skips_reset(self):
        import types
        fake = types.ModuleType('services.snowluma')
        fake.accounts = lambda: {'test': {'container': 'shared', 'program': 'qq-test', 'uin': '123'}}
        fake._container_client = lambda _: object()
        fake._ensure_extractor = lambda _: None
        fake._states_cache = {}
        fake._win_cache = {'ts': 0}
        fake._account_login_stage = lambda _: 'waiting'
        fake.fetch_qr = lambda _: {'status': 'ok'}
        state = types.ModuleType('services.snowluma_state')
        state._cache = {}
        logger = types.ModuleType('services.operation_logger')
        logger.operation_logger = types.SimpleNamespace(info=lambda *a: None)
        job = {'name': 'test'}
        with patch.dict(sys.modules, {'services.snowluma': fake, 'services.snowluma_state': state,
                                     'services.operation_logger': logger}), \
             patch.object(repair, '_helper') as helper_call:
            repair._run(job)
        self.assertEqual(job['result'], 'already_available')
        helper_call.assert_not_called()

    def test_finalize_records_other_program_changes(self):
        with tempfile.TemporaryDirectory() as d:
            transaction = Path(d)
            data = {'phase': 'verifying', 'before': {'qq-test': ['RUNNING', '1'], 'qq-other': ['RUNNING', '2']}}
            with patch.object(helper, 'load', return_value=(data, Path(d), Path(d) / 'QQ')), \
                 patch.object(helper, 'snapshot', return_value={'qq-test': ['RUNNING', '3'], 'qq-other': ['RUNNING', '2']}):
                result = helper.finish(transaction, 'qq-test', '123')
            self.assertEqual(result['other_program_changes'], [])
            self.assertTrue((transaction / 'manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
