import unittest
from datetime import datetime, timedelta, timezone

from src.durations import deletion_deadline, parse_duration_minutes
from src.permissions import Role, check_admin_change, is_allowed, role_for

OWNER, ADMIN, STRANGER = 1, 2, 3
PUBLISHED = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)


class DurationTests(unittest.TestCase):
    def test_45_hours_accepted(self):
        self.assertEqual(parse_duration_minutes("45h"), 2700)

    def test_above_45_hours_rejected(self):
        for raw in ("46h", "2701", "3000m", "50h", "100h"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_duration_minutes(raw)

    def test_zero_negative_malformed_rejected(self):
        for raw in ("0", "0h", "-5", "-1h", "abc", "", "1.5h", "h"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_duration_minutes(raw)

    def test_one_minute_minimum_accepted(self):
        self.assertEqual(parse_duration_minutes("1"), 1)

    def test_auto_deadline_rejects_over_45_hours(self):
        with self.assertRaises(ValueError):
            deletion_deadline(PUBLISHED, 60 * 100)

    def test_auto_deadline_at_45_hours_exact(self):
        self.assertEqual(deletion_deadline(PUBLISHED, 2700), PUBLISHED + timedelta(hours=45))

    def test_short_auto_deadline_respected(self):
        self.assertEqual(deletion_deadline(PUBLISHED, 120), PUBLISHED + timedelta(hours=2))

    def test_manual_mode_keeps_45_hour_safety_deadline(self):
        self.assertEqual(deletion_deadline(PUBLISHED, None), PUBLISHED + timedelta(hours=45))


class PermissionTests(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(role_for(OWNER, OWNER, {ADMIN}), Role.OWNER)
        self.assertEqual(role_for(ADMIN, OWNER, {ADMIN}), Role.ADMIN)
        self.assertEqual(role_for(STRANGER, OWNER, {ADMIN}), Role.NONE)

    def test_stranger_and_admin_access_levels(self):
        self.assertFalse(is_allowed(Role.NONE, Role.ADMIN))
        self.assertTrue(is_allowed(Role.ADMIN, Role.ADMIN))
        self.assertFalse(is_allowed(Role.ADMIN, Role.OWNER))

    def test_admin_cannot_manage_admins(self):
        with self.assertRaises(PermissionError):
            check_admin_change(Role.ADMIN, STRANGER, OWNER)

    def test_owner_cannot_be_targeted(self):
        with self.assertRaises(PermissionError):
            check_admin_change(Role.OWNER, OWNER, OWNER)

    def test_owner_can_manage_others(self):
        check_admin_change(Role.OWNER, STRANGER, OWNER)


if __name__ == "__main__":
    unittest.main()
