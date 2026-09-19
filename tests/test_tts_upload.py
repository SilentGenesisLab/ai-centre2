from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException, UploadFile

from control_plane.api import _save_reference_audio


class TTSReferenceUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_supported_reference_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wav = b"RIFF\x24\x00\x00\x00WAVEaudio"
            upload = UploadFile(file=io.BytesIO(wav), filename="voice.wav")

            path = await _save_reference_audio(upload, Path(directory))

            self.assertEqual(path.read_bytes(), wav)
            self.assertEqual(path.suffix, ".wav")

    async def test_uppercase_mp3_is_accepted_by_file_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00audio"
            upload = UploadFile(
                file=io.BytesIO(mp3),
                filename="VOICE.MP3",
            )

            path = await _save_reference_audio(upload, Path(directory))

            self.assertEqual(path.read_bytes(), mp3)
            self.assertEqual(path.suffix, ".mp3")

    async def test_rejects_unsupported_extension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(file=io.BytesIO(b"data"), filename="voice.exe")

            with self.assertRaises(HTTPException) as raised:
                await _save_reference_audio(upload, Path(directory))

            self.assertEqual(raised.exception.status_code, 415)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_rejects_spoofed_uppercase_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(file=io.BytesIO(b"not an mp3"), filename="voice.MP3")

            with self.assertRaises(HTTPException) as raised:
                await _save_reference_audio(upload, Path(directory))

            self.assertEqual(raised.exception.status_code, 415)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_removes_partial_file_when_upload_is_too_large(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            upload = UploadFile(file=io.BytesIO(b"12345"), filename="voice.wav")

            with self.assertRaises(HTTPException) as raised:
                await _save_reference_audio(upload, Path(directory), max_bytes=4)

            self.assertEqual(raised.exception.status_code, 413)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
