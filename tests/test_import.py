import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('manager', Path(__file__).resolve().parents[1] / 'backend/awg_manager.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(manager, 'STATE', self.root), patch.object(manager, 'DB', self.root / 'manager.db')]
        for item in self.patches:
            item.start()
        self.db = manager.connect()
        self.config = self.root / 'awg0.conf'

    def tearDown(self):
        self.db.close()
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def fixture(self, allowed='10.66.66.2/32', extra=''):
        self.config.write_text('[Interface]\nAddress = 10.66.66.1/24\n[Peer]\nPublicKey = test-public\nAllowedIPs = ' + allowed + '\n' + extra)

    def test_import_preserves_peer_and_is_idempotent(self):
        self.fixture()
        before = self.config.read_bytes()
        self.assertEqual(manager.import_server_config(self.db, self.config)['imported'], 1)
        self.assertEqual(manager.import_server_config(self.db, self.config)['imported'], 0)
        peer = self.db.execute('SELECT * FROM peers').fetchone()
        self.assertEqual(peer['public_key'], 'test-public')
        self.assertFalse(manager.public_peer(peer)['exportable'])
        with self.assertRaises(ValueError):
            manager.export_config(peer)
        self.assertEqual(self.config.read_bytes(), before)

    def test_unsupported_peers_are_rejected_not_silently_deleted(self):
        for allowed in ['10.66.66.0/24', '10.66.66.2/32, ::/0', '192.168.1.2/32']:
            self.fixture(allowed)
            with self.assertRaises(ValueError):
                manager.import_server_config(self.db, self.config)

    def test_unknown_settings_reject_import(self):
        self.fixture(extra='PersistentKeepalive = 25\n')
        with self.assertRaises(ValueError):
            manager.import_server_config(self.db, self.config)

    def test_expired_period_resets_blocked_peer_usage(self):
        self.fixture()
        manager.import_server_config(self.db, self.config)
        self.db.execute("UPDATE peers SET quota_period='day',quota_period_key='2000-01-01',quota_used=100")
        with patch.object(manager, 'awg_stats', return_value={}):
            manager.sample_usage(self.db, manager.now_ts())
        self.assertEqual(self.db.execute('SELECT quota_used FROM peers').fetchone()[0], 0)

    def test_sunday_range(self):
        self.assertEqual(manager.cron_values('1-7', 0, 7, True), set(range(7)))


if __name__ == '__main__':
    unittest.main()
