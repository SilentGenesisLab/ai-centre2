from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile

from managed_backends.musetalk_api import (
    AUDIO_SUFFIXES,
    VIDEO_SUFFIXES,
    MuseTalkJobs,
    OssResultPublisher,
    LipSyncUrlRequest,
    checked_suffix,
    create_url_job,
    require_service_token,
    save_upload,
)
from control_plane.media_fetch import DownloadedMedia, MediaFetchError


class MuseTalkApiTests(unittest.TestCase):
    def test_oss_result_publisher_uploads_to_stable_public_url(self) -> None:
        job_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "result.mp4"
            result_path.write_bytes(b"video")
            with (
                patch.dict(
                    os.environ,
                    {
                        "MUSETALK_OSS_ENABLED": "true",
                        "OSS_ACCESS_KEY_ID": "test-id",
                        "OSS_ACCESS_KEY_SECRET": "test-secret",
                        "OSS_ENDPOINT": "oss-cn-shenzhen.aliyuncs.com",
                        "OSS_BUCKET": "media-bucket",
                        "OSS_PUBLIC_BASE_URL": "https://media.example.com/",
                        "MUSETALK_OSS_PREFIX": "/ai-centre/lipsync/",
                    },
                    clear=True,
                ),
                patch("managed_backends.musetalk_api.oss2.Bucket") as bucket_class,
            ):
                bucket_class.return_value.put_object_from_file.return_value.status = 200
                publisher = OssResultPublisher.from_env()
                self.assertIsNotNone(publisher)
                url = publisher.upload(job_id, result_path)

            self.assertEqual(
                url,
                f"https://media.example.com/ai-centre/lipsync/{job_id}/result.mp4",
            )
            args, kwargs = bucket_class.return_value.put_object_from_file.call_args
            self.assertEqual(args[0], f"ai-centre/lipsync/{job_id}/result.mp4")
            self.assertEqual(args[1], str(result_path))
            self.assertEqual(kwargs["headers"]["Content-Type"], "video/mp4")

    def test_oss_result_publisher_is_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(OssResultPublisher.from_env())

    def test_token_requires_bearer_scheme(self) -> None:
        with patch.dict(os.environ, {"SERVICE_TOKEN": "secret"}):
            require_service_token("Bearer secret")
            with self.assertRaises(HTTPException) as raised:
                require_service_token("secret")
        self.assertEqual(raised.exception.status_code, 401)

    def test_file_types_are_allowlisted(self) -> None:
        self.assertEqual(checked_suffix("face.MP4", VIDEO_SUFFIXES, "video"), ".mp4")
        self.assertEqual(checked_suffix("speech.wav", AUDIO_SUFFIXES, "audio"), ".wav")
        with self.assertRaises(HTTPException) as raised:
            checked_suffix("payload.py", VIDEO_SUFFIXES, "video")
        self.assertEqual(raised.exception.status_code, 415)

    def test_upload_limit_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "input.mp4"
            upload = UploadFile(file=io.BytesIO(b"12345"), filename="input.mp4")
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(save_upload(upload, destination, 4))
            self.assertFalse(destination.exists())
        self.assertEqual(raised.exception.status_code, 413)

    def test_url_job_persists_downloads_without_source_urls(self) -> None:
        async def fake_download(_url, directory, stem, *_args, **_kwargs):
            suffix = ".mp4" if stem == "input-video" else ".wav"
            path = directory / f"{stem}{suffix}"
            path.write_bytes(stem.encode())
            return DownloadedMedia(path, path.stat().st_size, "application/octet-stream")

        with tempfile.TemporaryDirectory() as temporary:
            fake_jobs = MuseTalkJobs()
            fake_jobs.data_dir = Path(temporary)
            with (
                patch("managed_backends.musetalk_api.jobs", fake_jobs),
                patch(
                    "managed_backends.musetalk_api.download_public_media_async",
                    side_effect=fake_download,
                ),
            ):
                result = asyncio.run(
                    create_url_job(
                        LipSyncUrlRequest(
                            video_url="https://cdn.example/video.mp4?signature=secret",
                            audio_url="https://cdn.example/audio.wav?signature=secret",
                            face_restore=False,
                        )
                    )
                )

            persisted = fake_jobs.status(result["job_id"])
            self.assertNotIn("video_url", persisted)
            self.assertNotIn("audio_url", persisted)
            self.assertNotIn("secret", str(persisted))

    def test_url_job_cleans_directory_when_one_download_fails(self) -> None:
        async def fake_download(_url, directory, stem, *_args, **_kwargs):
            if stem == "input-audio":
                raise MediaFetchError(502, "remote media download failed")
            path = directory / "input-video.mp4"
            path.write_bytes(b"video")
            return DownloadedMedia(path, 5, "video/mp4")

        with tempfile.TemporaryDirectory() as temporary:
            fake_jobs = MuseTalkJobs()
            fake_jobs.data_dir = Path(temporary)
            with (
                patch("managed_backends.musetalk_api.jobs", fake_jobs),
                patch(
                    "managed_backends.musetalk_api.download_public_media_async",
                    side_effect=fake_download,
                ),
            ):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        create_url_job(
                            LipSyncUrlRequest(
                                video_url="https://cdn.example/video.mp4",
                                audio_url="https://cdn.example/audio.wav",
                            )
                        )
                    )

            self.assertEqual(raised.exception.status_code, 502)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_queued_job_can_be_cancelled_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"MUSETALK_DATA_DIR": temporary}):
                jobs = MuseTalkJobs()
            job_id = str(uuid.uuid4())
            jobs.submit(job_id, "face.mp4", "speech.wav", 10, 20, True)

            cancelled = jobs.cancel(job_id)

            self.assertEqual(cancelled["state"], "cancelled")
            self.assertTrue(cancelled["face_restore"])
            self.assertEqual(jobs.status(job_id)["state"], "cancelled")

    def test_running_job_is_requeued_after_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"MUSETALK_DATA_DIR": temporary}):
                jobs = MuseTalkJobs()
            job_id = str(uuid.uuid4())
            job_dir = Path(temporary) / job_id
            job_dir.mkdir()
            (job_dir / "input-video.mp4").write_bytes(b"video")
            (job_dir / "input-audio.wav").write_bytes(b"audio")
            (job_dir / "inference.log").write_text("previous attempt", encoding="utf-8")
            (job_dir / "output").mkdir()
            (job_dir / "output" / "partial.mp4").write_bytes(b"partial")
            jobs._write_status(
                {
                    "job_id": job_id,
                    "state": "running",
                    "stage": "gfpgan",
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "started_at": "2026-08-01T00:00:01+00:00",
                    "finished_at": None,
                    "musetalk_seconds": 12.3,
                    "error": None,
                }
            )

            with patch("managed_backends.musetalk_api.threading.Thread") as worker:
                jobs.start()

            recovered = jobs.status(job_id)
            self.assertEqual(recovered["state"], "queued")
            self.assertEqual(recovered["stage"], "queued")
            self.assertEqual(recovered["recovery_count"], 1)
            self.assertEqual(recovered["recovered_from_stage"], "gfpgan")
            self.assertIsNone(recovered["started_at"])
            self.assertNotIn("musetalk_seconds", recovered)
            self.assertEqual(jobs._queue.get_nowait(), job_id)
            self.assertTrue((job_dir / "inference.recovery-1.log").is_file())
            self.assertFalse((job_dir / "output").exists())
            worker.return_value.start.assert_called_once_with()

    def test_job_id_cannot_traverse_outside_job_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"MUSETALK_DATA_DIR": temporary}):
                jobs = MuseTalkJobs()
            with self.assertRaises(HTTPException) as raised:
                jobs.status("../../etc/passwd")
        self.assertEqual(raised.exception.status_code, 404)

    def test_jobs_are_listed_newest_first_and_can_be_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"MUSETALK_DATA_DIR": temporary}):
                jobs = MuseTalkJobs()
            older = str(uuid.uuid4())
            newer = str(uuid.uuid4())
            jobs._write_status(
                {
                    "job_id": older,
                    "state": "completed",
                    "created_at": "2026-08-01T00:00:00+00:00",
                }
            )
            jobs._write_status(
                {
                    "job_id": newer,
                    "state": "failed",
                    "created_at": "2026-08-02T00:00:00+00:00",
                }
            )

            listed = jobs.list_jobs(50)
            failed = jobs.list_jobs(50, "failed")

            self.assertEqual([item["job_id"] for item in listed["jobs"]], [newer, older])
            self.assertEqual(listed["total"], 2)
            self.assertEqual([item["job_id"] for item in failed["jobs"]], [newer])

    def test_job_logs_are_stage_allowlisted_and_tailed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"MUSETALK_DATA_DIR": temporary}):
                jobs = MuseTalkJobs()
            job_id = str(uuid.uuid4())
            job_dir = Path(temporary) / job_id
            job_dir.mkdir()
            (job_dir / "inference.log").write_text("one\ntwo\nthree\n", encoding="utf-8")

            result = jobs.logs(job_id, "musetalk", 2)

            self.assertEqual(result["lines"], 2)
            self.assertEqual(result["log"], "two\nthree\n")
            with self.assertRaises(HTTPException) as raised:
                jobs.logs(job_id, "../../secret", 10)
            self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
