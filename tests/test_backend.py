import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('awg_manager', ROOT / 'backend/awg_manager.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class BackendTests(unittest.TestCase):
    def test_common_cron(self):
        manager.parse_cron('0 8 * * 1-5')
        monday = dt.datetime(2026, 9, 7, 8, 0, tzinfo=ZoneInfo('Europe/Moscow'))
        sunday = dt.datetime(2026, 9, 6, 8, 0, tzinfo=ZoneInfo('Europe/Moscow'))
        self.assertTrue(manager.cron_matches('0 8 * * 1-5', monday))
        self.assertFalse(manager.cron_matches('0 8 * * 1-5', sunday))

    def test_cron_rejects_shell_text(self):
        with self.assertRaises(ValueError):
            manager.parse_cron('0 8 * * *; reboot')

    def test_name_validation(self):
        self.assertEqual(manager.validate_name('phone-1'), 'phone-1')
        for value in ('../key', 'имя', 'a b', ''):
            with self.assertRaises(ValueError):
                manager.validate_name(value)


if __name__ == '__main__':
    unittest.main()
