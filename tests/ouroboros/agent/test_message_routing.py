"""
Tests for v6 message routing: single-consumer delivery,
and per-task mailbox.

Run: pytest tests/ouroboros/agent/test_message_routing.py -v
"""

import json
import unittest

from tests.helpers.shared import TEST_SCOPE


class TestOwnerInjectPerTask(unittest.TestCase):
    """Test per-task mailbox in owner_inject.py."""

    def setUp(self):
        from ouroboros.storage.settings.paths import load_path_settings

        self.drive_root = load_path_settings().data_dir.resolve()

    def _stored_mailbox(self, task_id):
        from ouroboros.agent.owner_inject import _mailbox_path
        from ouroboros.storage.blob.drive import get_drive_storage

        path = _mailbox_path(self.drive_root, task_id)
        return get_drive_storage(self.drive_root), path

    def test_write_creates_a_per_task_mailbox(self):
        from ouroboros.agent.owner_inject import write_owner_message
        from ouroboros.utils import read_text

        write_owner_message(TEST_SCOPE, self.drive_root, "hello", task_id="abc123", msg_id="m1")

        drive, path = self._stored_mailbox("abc123")
        self.assertTrue(drive.path_exists(TEST_SCOPE, path))
        entry = json.loads(read_text(TEST_SCOPE, path).strip())
        self.assertEqual(entry["text"], "hello")
        self.assertEqual(entry["msg_id"], "m1")

    def test_drain_reads_only_own_task(self):
        from ouroboros.agent.owner_inject import drain_owner_messages, write_owner_message

        write_owner_message(TEST_SCOPE, self.drive_root, "for task A", task_id="taskA", msg_id="m1")
        write_owner_message(TEST_SCOPE, self.drive_root, "for task B", task_id="taskB", msg_id="m2")

        msgs_a = drain_owner_messages(TEST_SCOPE, self.drive_root, task_id="taskA")
        msgs_b = drain_owner_messages(TEST_SCOPE, self.drive_root, task_id="taskB")

        self.assertEqual(msgs_a, ["for task A"])
        self.assertEqual(msgs_b, ["for task B"])

    def test_drain_dedup_with_seen_ids(self):
        from ouroboros.agent.owner_inject import drain_owner_messages, write_owner_message

        write_owner_message(TEST_SCOPE, self.drive_root, "msg1", task_id="t1", msg_id="id1")
        write_owner_message(TEST_SCOPE, self.drive_root, "msg2", task_id="t1", msg_id="id2")

        seen = set()
        first_read = drain_owner_messages(TEST_SCOPE, self.drive_root, task_id="t1", seen_ids=seen)
        self.assertEqual(len(first_read), 2)
        self.assertEqual(seen, {"id1", "id2"})

        write_owner_message(TEST_SCOPE, self.drive_root, "msg3", task_id="t1", msg_id="id3")
        second_read = drain_owner_messages(TEST_SCOPE, self.drive_root, task_id="t1", seen_ids=seen)
        self.assertEqual(second_read, ["msg3"])
        self.assertIn("id3", seen)

    def test_cleanup_removes_the_mailbox(self):
        from ouroboros.agent.owner_inject import cleanup_task_mailbox, write_owner_message

        write_owner_message(TEST_SCOPE, self.drive_root, "hello", task_id="t1", msg_id="m1")
        drive, path = self._stored_mailbox("t1")
        self.assertTrue(drive.path_exists(TEST_SCOPE, path))

        cleanup_task_mailbox(TEST_SCOPE, self.drive_root, "t1")
        self.assertFalse(drive.path_exists(TEST_SCOPE, path))

    def test_drain_nonexistent_task_returns_empty(self):
        from ouroboros.agent.owner_inject import drain_owner_messages

        msgs = drain_owner_messages(TEST_SCOPE, self.drive_root, task_id="nonexistent")
        self.assertEqual(msgs, [])

    def test_messages_not_cleared_on_read(self):
        """Messages persist after read (append-only). Only cleanup removes them."""
        from ouroboros.agent.owner_inject import drain_owner_messages, write_owner_message
        from ouroboros.utils import read_text

        write_owner_message(TEST_SCOPE, self.drive_root, "persistent", task_id="t1", msg_id="m1")

        drain_owner_messages(TEST_SCOPE, self.drive_root, task_id="t1")

        drive, path = self._stored_mailbox("t1")
        self.assertTrue(drive.path_exists(TEST_SCOPE, path))
        self.assertIn("persistent", read_text(TEST_SCOPE, path))


if __name__ == "__main__":
    unittest.main()
