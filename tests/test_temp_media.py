from __future__ import annotations

import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import temp_media


class TempMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.original_root = temp_media.TEMP_DATA_ROOT
        self.root = Path(self.temporary.name) / "temp-data"
        self.root.mkdir()
        temp_media.TEMP_DATA_ROOT = self.root

    def tearDown(self) -> None:
        temp_media.TEMP_DATA_ROOT = self.original_root
        self.temporary.cleanup()

    def test_daily_directory_uses_shanghai_date(self) -> None:
        instant = datetime(2026, 8, 18, 16, 30, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(temp_media.daily_directory(instant), self.root / "20260819")
        self.assertEqual((self.root / "20260819").stat().st_mode & 0o777, 0o770)

    def test_sanitizes_name_and_uses_detected_suffix(self) -> None:
        path = temp_media.allocate_path("../../泰国-8.exe", suffix=".flac")
        expected_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        self.assertEqual(path.parent, self.root / expected_date)
        prefix, name = path.name.split("-", 1)
        self.assertEqual(len(prefix), 32)
        int(prefix, 16)
        self.assertEqual(name, "泰国-8.flac")
        self.assertEqual(path.stat().st_mode & 0o777, 0o660)

    def test_ten_thousand_names_are_unique(self) -> None:
        paths = [temp_media.allocate_path("同名视频.mp4") for _ in range(10_000)]
        self.assertEqual(len({path.name for path in paths}), 10_000)

    def test_concurrent_allocations_are_unique(self) -> None:
        with ThreadPoolExecutor(max_workers=32) as executor:
            paths = list(
                executor.map(lambda _: temp_media.allocate_path("source.wav"), range(512))
            )
        self.assertEqual(len({path.name for path in paths}), 512)

    def test_success_removes_media_immediately(self) -> None:
        path = temp_media.write_bytes("audio.wav", b"payload")
        lease = temp_media.lease_marker(path)
        self.assertTrue(lease.is_file())
        temp_media.cleanup_success(path)
        self.assertFalse(path.exists())
        self.assertFalse(temp_media.failed_marker(path).exists())
        self.assertFalse(lease.exists())

    def test_failure_is_retained_until_expiry(self) -> None:
        path = temp_media.write_bytes("audio.wav", b"payload")
        temp_media.mark_failed(path, RuntimeError("boom"))
        marker = temp_media.failed_marker(path)
        self.assertFalse(temp_media.lease_marker(path).exists())
        now = time.time()
        os.utime(path, (now - 90_000, now - 90_000))
        os.utime(marker, (now - 3600, now - 3600))
        temp_media.cleanup_expired(
            root=self.root,
            retention_seconds=86_400,
            now_timestamp=now,
        )
        self.assertTrue(path.exists())
        self.assertTrue(marker.exists())
        os.utime(marker, (now - 90_000, now - 90_000))
        temp_media.cleanup_expired(
            root=self.root,
            retention_seconds=86_400,
            now_timestamp=now,
        )
        self.assertFalse(path.exists())
        self.assertFalse(marker.exists())

    def test_orphan_is_removed_after_retention(self) -> None:
        path = temp_media.write_bytes("orphan.mp4", b"payload")
        lease = temp_media.lease_marker(path)
        now = time.time()
        os.utime(path, (now - 90_000, now - 90_000))
        os.utime(lease, (now - 90_000, now - 90_000))
        result = temp_media.cleanup_expired(
            root=self.root,
            retention_seconds=86_400,
            now_timestamp=now,
        )
        self.assertEqual(result["removed_files"], 2)
        self.assertFalse(path.exists())
        self.assertFalse(lease.exists())

    def test_work_directory_is_uuid_prefixed(self) -> None:
        directory = temp_media.allocate_work_directory("precise-subtitle")
        expected_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        self.assertEqual(directory.parent.name, expected_date)
        prefix, label = directory.name.split("-", 1)
        self.assertEqual(len(prefix), 32)
        self.assertEqual(label, "precise-subtitle")
        self.assertTrue(temp_media.lease_marker(directory).is_file())

    def test_success_cleans_failed_work_directory_marker(self) -> None:
        directory = temp_media.allocate_work_directory("retry")
        temp_media.mark_failed(directory, RuntimeError("first attempt"))
        marker = temp_media.failed_marker(directory)
        self.assertTrue(marker.is_file())

        temp_media.cleanup_success(directory)

        self.assertFalse(directory.exists())
        self.assertFalse(marker.exists())
