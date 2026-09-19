from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

import httpx


MAX_MEDIA_URL_LENGTH = 4096
GENERIC_CONTENT_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
}


class MediaFetchError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class MediaSpec:
    suffixes: frozenset[str]
    content_types: Mapping[str, str]
    equivalent_suffixes: tuple[frozenset[str], ...] = ()


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    size: int
    content_type: str


VIDEO_MEDIA = MediaSpec(
    suffixes=frozenset({".mp4", ".mov", ".mkv", ".webm"}),
    content_types={
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
        "video/webm": ".webm",
    },
)
CUBE_MEDIA = MediaSpec(
    suffixes=frozenset({".cube"}),
    content_types={
        "text/plain": ".cube",
        "application/octet-stream": ".cube",
        "application/x-cube": ".cube",
    },
)
AUDIO_MEDIA = MediaSpec(
    suffixes=frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}),
    content_types={
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
        "audio/flac": ".flac",
        "audio/x-flac": ".flac",
        "audio/ogg": ".ogg",
        "application/ogg": ".ogg",
    },
)
ASR_MEDIA = MediaSpec(
    suffixes=VIDEO_MEDIA.suffixes | AUDIO_MEDIA.suffixes,
    content_types={**VIDEO_MEDIA.content_types, **AUDIO_MEDIA.content_types},
)
IMAGE_MEDIA = MediaSpec(
    suffixes=frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"}),
    content_types={
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    },
    equivalent_suffixes=(frozenset({".jpg", ".jpeg"}),),
)


Resolver = Callable[[str, int], list[str]]


def resolve_public_ips(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise MediaFetchError(422, "media URL hostname cannot be resolved") from exc
    addresses = {record[4][0].split("%", 1)[0] for record in records}
    if not addresses:
        raise MediaFetchError(422, "media URL hostname cannot be resolved")
    return _require_public_ips(addresses)


def _require_public_ips(
    addresses,
) -> list[str]:
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise MediaFetchError(422, "media URL resolved to an invalid address") from exc
        if not ip.is_global:
            raise MediaFetchError(422, "media URL must not resolve to a private address")
        parsed.append(ip)
    parsed.sort(key=lambda item: (item.version, int(item)))
    return [str(item) for item in parsed]


def _validated_target(url: str, resolver: Resolver) -> tuple[httpx.URL, str, str]:
    if not url or len(url) > MAX_MEDIA_URL_LENGTH:
        raise MediaFetchError(422, "media URL is empty or too long")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise MediaFetchError(422, "media URL must use HTTPS")
    if parsed.username or parsed.password:
        raise MediaFetchError(422, "media URL must not contain credentials")
    if parsed.fragment:
        raise MediaFetchError(422, "media URL must not contain a fragment")
    host = parsed.hostname
    if not host:
        raise MediaFetchError(422, "media URL hostname is required")
    try:
        ascii_host = host.encode("idna").decode("ascii")
        port = parsed.port or 443
    except (UnicodeError, ValueError) as exc:
        raise MediaFetchError(422, "media URL hostname or port is invalid") from exc
    addresses = resolver(ascii_host, port)
    if not addresses:
        raise MediaFetchError(422, "media URL hostname cannot be resolved")
    addresses = _require_public_ips(addresses)
    try:
        pinned = httpx.URL(url).copy_with(host=addresses[0])
    except (TypeError, ValueError) as exc:
        raise MediaFetchError(422, "media URL is invalid") from exc
    host_value = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    if port != 443:
        host_value = f"{host_value}:{port}"
    return pinned, ascii_host, host_value


def validate_public_https_url(url: str, resolver: Resolver = resolve_public_ips) -> None:
    _validated_target(url, resolver)


def sniff_media_suffix(data: bytes) -> str | None:
    """Return a conservative media suffix from the file signature."""
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"fLaC"):
        return ".flac"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"ID3"):
        return ".mp3"
    if len(data) >= 2 and data[0] == 0xFF:
        if data[1] & 0xF6 == 0xF0:
            return ".aac"
        if data[1] & 0xE0 == 0xE0 and data[1] & 0x06:
            return ".mp3"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return ".mkv"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"M4A ", b"M4B ", b"M4P "}:
            return ".m4a"
        if brand == b"qt  ":
            return ".mov"
        return ".mp4"
    return None


def _suffix_for_response(
    url: str,
    content_type: str,
    spec: MediaSpec,
    signature: bytes = b"",
) -> str:
    suffix = Path(unquote(urlsplit(url).path)).suffix.lower()
    media_type = content_type.split(";", 1)[0].strip().lower()
    detected = sniff_media_suffix(signature)
    if detected in spec.suffixes:
        return detected
    if detected is not None:
        raise MediaFetchError(415, "remote media file header is unsupported")
    if spec is AUDIO_MEDIA or spec is ASR_MEDIA:
        raise MediaFetchError(415, "remote media file header is not recognized")
    if suffix and suffix not in spec.suffixes:
        raise MediaFetchError(415, "remote media file type is unsupported")
    if media_type in GENERIC_CONTENT_TYPES or not media_type:
        if suffix:
            return suffix
        raise MediaFetchError(415, "remote media response has no supported file type")
    canonical = spec.content_types.get(media_type)
    if canonical is None:
        raise MediaFetchError(415, "remote media content type is unsupported")
    if not suffix or suffix == canonical:
        return suffix or canonical
    if any({suffix, canonical}.issubset(group) for group in spec.equivalent_suffixes):
        return suffix
    raise MediaFetchError(415, "remote media extension conflicts with its content type")


def download_public_media(
    url: str,
    directory: Path,
    stem: str,
    spec: MediaSpec,
    max_bytes: int,
    timeout_seconds: float,
    *,
    max_redirects: int = 3,
    resolver: Resolver = resolve_public_ips,
    transport: httpx.BaseTransport | None = None,
) -> DownloadedMedia:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{stem}-{uuid4().hex}.part"
    current_url = url
    timeout = httpx.Timeout(timeout_seconds, connect=15)
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            for redirect_count in range(max_redirects + 1):
                pinned_url, sni_host, host_header = _validated_target(
                    current_url,
                    resolver,
                )
                try:
                    stream = client.stream(
                        "GET",
                        pinned_url,
                        headers={
                            "Host": host_header,
                            "Accept-Encoding": "identity",
                        },
                        extensions={"sni_hostname": sni_host},
                    )
                    with stream as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or redirect_count >= max_redirects:
                                raise MediaFetchError(502, "remote media has too many redirects")
                            current_url = urljoin(current_url, location)
                            continue
                        if not response.is_success:
                            raise MediaFetchError(502, "remote media download failed")
                        content_type = response.headers.get("content-type", "")
                        try:
                            declared_size = int(response.headers.get("content-length", "0"))
                        except ValueError:
                            declared_size = 0
                        if declared_size > max_bytes:
                            raise MediaFetchError(413, "remote media is too large")
                        size = 0
                        signature = bytearray()
                        with temporary.open("wb") as output:
                            for chunk in response.iter_bytes(1024 * 1024):
                                size += len(chunk)
                                if size > max_bytes:
                                    raise MediaFetchError(413, "remote media is too large")
                                if len(signature) < 64:
                                    signature.extend(chunk[: 64 - len(signature)])
                                output.write(chunk)
                        if not size:
                            raise MediaFetchError(400, "remote media is empty")
                        suffix = _suffix_for_response(
                            current_url,
                            content_type,
                            spec,
                            bytes(signature),
                        )
                        target = directory / f"{stem}{suffix}"
                        temporary.replace(target)
                        return DownloadedMedia(target, size, content_type)
                except httpx.TimeoutException as exc:
                    raise MediaFetchError(504, "remote media download timed out") from exc
                except httpx.RequestError as exc:
                    raise MediaFetchError(502, "remote media download failed") from exc
    finally:
        temporary.unlink(missing_ok=True)
    raise MediaFetchError(502, "remote media download failed")


async def download_public_media_async(*args, **kwargs) -> DownloadedMedia:
    return await asyncio.to_thread(download_public_media, *args, **kwargs)
