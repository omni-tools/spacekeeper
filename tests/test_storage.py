from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from spacekeeper.storage import SnapshotStore, StorageError, StorageFailure as Failure
from tests.support import observe, window


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "state"
        self.store = SnapshotStore(self.root)
        self.snapshot = observe(window())

    def test_default_replaces_automatically_and_named_requires_force(self):
        self.store.save(self.snapshot)
        newer = observe(window(fullscreen=True))
        self.store.save(newer)
        self.assertEqual(self.store.load(), newer)
        self.store.save(self.snapshot, "work")
        with self.assertRaises(StorageError) as caught:
            self.store.save(newer, "work")
        self.assertEqual(caught.exception.reason, Failure.EXISTS)
        self.assertEqual(self.store.load("work"), self.snapshot)
        self.store.save(newer, "work", force=True)
        self.assertEqual(self.store.load("work"), newer)

    def test_names_cannot_escape_the_storage_directory(self):
        for name in ("", "..", "../escape", "/tmp/escape", "a/b", "a\\b", ".hidden", "a" * 65, "name\n"):
            with self.subTest(name=name), self.assertRaises(StorageError) as caught:
                self.store.save(self.snapshot, name)
            self.assertEqual(caught.exception.reason, Failure.INVALID_NAME)
        self.assertFalse(self.root.exists())

    def test_listing_and_missing_load_do_not_create_directories(self):
        self.assertEqual(self.store.entries(), ())
        with self.assertRaises(StorageError) as caught:
            self.store.load()
        self.assertEqual(caught.exception.reason, Failure.NOT_FOUND)
        self.assertFalse(self.root.exists())

    def test_list_reports_corrupt_snapshots_without_hiding_valid_entries(self):
        self.store.save(self.snapshot, "work")
        (self.root / "snapshots/broken.json").write_text("not JSON")
        entries = self.store.entries()
        self.assertEqual([e.name for e in entries], ["broken", "work"])
        self.assertEqual(entries[0].error, Failure.INVALID_SNAPSHOT)
        self.assertEqual(entries[1].windows, 1)

    def test_atomic_replace_failure_preserves_previous_file_and_removes_temp(self):
        path = self.store.save(self.snapshot)
        original = path.read_bytes()
        with patch("spacekeeper.storage.os.replace", side_effect=OSError("Synthetic write failure")):
            with self.assertRaises(StorageError):
                self.store.save(observe())
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_concurrent_named_saves_have_one_winner(self):
        self.store.save(self.snapshot, "seed")
        def save(_):
            try:
                self.store.save(self.snapshot, "shared")
                return "saved"
            except StorageError as error:
                return error.reason
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(4)))
        self.assertEqual(results.count("saved"), 1)
        self.assertEqual(results.count(Failure.EXISTS), 3)
        self.assertEqual(self.store.load("shared"), self.snapshot)

    def test_private_files_and_directories(self):
        path = self.store.save(self.snapshot)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_snapshot_symlinks_are_not_followed_or_replaced(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text(self.snapshot.to_json())
        self.store.save(self.snapshot, "seed")
        path = self.root / "snapshots/linked.json"
        path.symlink_to(outside)
        with self.assertRaises(StorageError):
            self.store.load("linked")
        with self.assertRaises(StorageError):
            self.store.save(observe(), "linked", force=True)
        self.assertEqual(outside.read_text(), self.snapshot.to_json())

    def test_subdirectory_symlinks_are_rejected(self):
        self.root.mkdir()
        (self.root / "snapshots").symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(StorageError) as caught:
            self.store.entries()
        self.assertEqual(caught.exception.reason, Failure.UNSAFE_PATH)

    def test_non_regular_snapshot_does_not_block(self):
        self.store.save(self.snapshot, "seed")
        os.mkfifo(self.root / "snapshots/pipe.json")
        with self.assertRaises(StorageError) as caught:
            self.store.load("pipe")
        self.assertEqual(caught.exception.reason, Failure.UNSAFE_PATH)

    def test_size_limit_is_enforced_on_read_and_write(self):
        self.store.save(self.snapshot)
        small = SnapshotStore(self.root, size_limit=10)
        for operation in (lambda: small.load(), lambda: small.save(self.snapshot)):
            with self.assertRaises(StorageError) as caught:
                operation()
            self.assertEqual(caught.exception.reason, Failure.INVALID_SNAPSHOT)

    def test_backups_are_unique_validated_and_separate_from_names(self):
        first = self.store.backup(self.snapshot)
        second = self.store.backup(self.snapshot)
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.load_backup(first.stem), self.snapshot)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(self.store.entries(), ())
        report = self.store.report(first, {"complete": True})
        self.assertEqual(report.parent, self.root / "reports")
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)

    def test_backup_readback_failure_prevents_success(self):
        with patch.object(self.store, "_read", side_effect=StorageError(Failure.IO_ERROR, "Synthetic read failure")):
            with self.assertRaises(StorageError):
                self.store.backup(self.snapshot)
