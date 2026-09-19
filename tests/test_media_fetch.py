from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from control_plane.media_fetch import (
    AUDIO_MEDIA,
    IMAGE_MEDIA,
    MediaFetchError,
    download_public_media,
    resolve_public_ips,
    sniff_media_suffix,
    validate_public_https_url,
)


PUBLIC_IP = "93.184.216.34"


def public_resolver(_host: str, _port: int) -> list[str]:
    return [PUBLIC_IP]


class MediaFetchTests(unittest.TestCase):
    def test_sniffs_common_mp3_headers(self) -> None:
        self.assertEqual(sniff_media_suffix(b"ID3\x04\x00\x00\x00\x00\x00\x00"), ".mp3")
        self.assertEqual(sniff_media_suffix(b"\xff\xfb\x90\x64"), ".mp3")

    def test_audio_uses_file_header_for_uppercase_or_missing_suffix(self) -> None:
        mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00audio"
        for url, content_type in (
            ("https://cdn.example.com/reference.MP3", "application/octet-stream"),
            ("https://cdn.example.com/signed/reference?token=secret", ""),
        ):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                transport = httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        headers={"content-type": content_type},
                        content=mp3,
                    )
                )
                result = download_public_media(
                    url,
                    Path(directory),
                    "reference",
                    AUDIO_MEDIA,
                    1024,
                    30,
                    resolver=public_resolver,
                    transport=transport,
                )

                self.assertEqual(result.path.name, "reference.mp3")
                self.assertEqual(result.path.read_bytes(), mp3)

    def test_audio_rejects_spoofed_extension_and_content_type(self) -> None:
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "audio/mpeg"},
                content=b"this is not audio",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MediaFetchError) as raised:
                download_public_media(
                    "https://cdn.example.com/fake.MP3",
                    Path(directory),
                    "reference",
                    AUDIO_MEDIA,
                    1024,
                    30,
                    resolver=public_resolver,
                    transport=transport,
                )

            self.assertEqual(raised.exception.status_code, 415)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_non_https_credentials_and_private_addresses(self) -> None:
        with self.assertRaises(MediaFetchError):
            validate_public_https_url("http://example.com/image.png", public_resolver)
        with self.assertRaises(MediaFetchError):
            validate_public_https_url(
                "https://user:password@example.com/image.png",
                public_resolver,
            )
        with self.assertRaises(MediaFetchError):
            validate_public_https_url(
                "https://localhost/image.png",
                lambda _host, _port: ["127.0.0.1"],
            )
        with self.assertRaises(MediaFetchError):
            validate_public_https_url(
                "https://metadata.google.internal/image.png",
                lambda _host, _port: ["169.254.169.254"],
            )
        with self.assertRaises(MediaFetchError):
            validate_public_https_url(
                "https://example.com/image.png",
                lambda _host, _port: ["fd00::1"],
            )

    def test_resolver_rejects_if_any_dns_answer_is_private(self) -> None:
        records = [
            (2, 1, 6, "", (PUBLIC_IP, 443)),
            (2, 1, 6, "", ("10.0.0.2", 443)),
        ]
        with patch("socket.getaddrinfo", return_value=records):
            with self.assertRaises(MediaFetchError):
                resolve_public_ips("example.com", 443)

    def test_download_pins_public_ip_and_preserves_host_header(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, PUBLIC_IP)
            self.assertEqual(request.headers["host"], "cdn.example.com")
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"png-data",
            )

        with tempfile.TemporaryDirectory() as directory:
            result = download_public_media(
                "https://cdn.example.com/signed/image.png?token=secret",
                Path(directory),
                "image-1",
                IMAGE_MEDIA,
                1024,
                30,
                resolver=public_resolver,
                transport=httpx.MockTransport(handler),
            )

            self.assertEqual(result.path.name, "image-1.png")
            self.assertEqual(result.path.read_bytes(), b"png-data")

    def test_redirect_is_revalidated_and_private_target_is_rejected(self) -> None:
        def resolver(host: str, _port: int) -> list[str]:
            return ["127.0.0.1"] if host == "internal.example" else [PUBLIC_IP]

        transport = httpx.MockTransport(
            lambda _request: httpx.Response(
                302,
                headers={"location": "https://internal.example/image.png"},
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MediaFetchError) as raised:
                download_public_media(
                    "https://cdn.example.com/image.png",
                    Path(directory),
                    "image-1",
                    IMAGE_MEDIA,
                    1024,
                    30,
                    resolver=resolver,
                    transport=transport,
                )

            self.assertEqual(raised.exception.status_code, 422)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_rejects_oversize_empty_and_type_conflict_without_partial_files(self) -> None:
        cases = [
            ({"content-type": "image/png", "content-length": "5"}, b"12345", 4, 413),
            ({"content-type": "image/png"}, b"", 10, 400),
            ({"content-type": "image/jpeg"}, b"png", 10, 415),
        ]
        for headers, content, limit, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                transport = httpx.MockTransport(
                    lambda _request: httpx.Response(200, headers=headers, content=content)
                )
                with self.assertRaises(MediaFetchError) as raised:
                    download_public_media(
                        "https://cdn.example.com/image.png",
                        Path(directory),
                        "image-1",
                        IMAGE_MEDIA,
                        limit,
                        30,
                        resolver=public_resolver,
                        transport=transport,
                    )
                self.assertEqual(raised.exception.status_code, expected)
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
