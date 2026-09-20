from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo


TEMP_DATA_ROOT = Path(os.getenv("AI_CENTRE_TEMP_DATA_ROOT", "/home/donxu/temp-data"))
TEMP_DATA_TIMEZONE = ZoneInfo(os.getenv("AI_CENTRE_TEMP_DATA_TIMEZONE", "Asia/Shanghai"))
FAILURE_RETENTION_SECONDS = int(
    os.getenv("AI_CENTRE_TEMP_FAILURE_RETENTION_SECONDS", "86400")
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,16}$")


def _now() -> datetime:
    return datetime.now(TEMP_DATA_TIMEZONE)


def daily_directory(now: datetime | None = None, *, root: Path | None = None) -> Path:
    moment = now or _now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=TEMP_DATA_TIMEZONE)
    base = root or TEMP_DATA_ROOT
    directory = base / moment.astimezone(TEMP_DATA_TIMEZONE).strftime("%Y%m%d")
    directory.mkdir(parents=True, exist_ok=True, mode=0o770)
    directory.chmod(0o770)
    return directory


def sanitize_original_name(
    original_name: str | None,
    *,
    fallback: str = "media.bin",
    suffix: str | None = None,
) -> str:
    raw = (original_name or fallback).replace("\\", "/")
    name = raw.rsplit("/", 1)[-1]
    name = _CONTROL_CHARACTERS.sub("_", name).strip().lstrip(".")
    if not name or name in {".", ".."}:
        name = fallback
    name = name.replace("/", "_").replace("\\", "_")
    if suffix is not None:
        normalized_suffix = suffix.lower()
        if not normalized_suffix.startswith("."):
            normalized_suffix = f".{normalized_suffix}"
        if not _SAFE_SUFFIX.fullmatch(normalized_suffix):
            raise ValueError("unsafe media suffix")
        stem = Path(name).stem or Path(fallback).stem or "media"
        name = f"{stem}{normalized_suffix}"
    if len(name) > 180:
        extension = Path(name).suffix
        keep = max(1, 180 - len(extension))
        name = f"{Path(name).stem[:keep]}{extension}"
    return name


def allocate_path(
    original_name: str | None,
    *,
    suffix: str | None = None,
    reserve: bool = True,
) -> Path:
    safe_name = sanitize_original_name(original_name, suffix=suffix)
    directory = daily_directory()
    for _ in range(32):
        path = directory / f"{uuid4().hex}-{safe_name}"
        if not reserve:
            if not path.exists():
                return path
            continue
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660)
        except FileExistsError:
            continue
        os.close(descriptor)
        path.chmod(0o660)
        try:
            _create_active_lease(path)
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise
    raise RuntimeError("unable to allocate a unique temporary media path")


def write_bytes(
    original_name: str | None,
    content: bytes,
    *,
    suffix: str | None = None,
) -> Path:
    path = allocate_path(original_name, suffix=suffix)
    try:
        path.write_bytes(content)
        path.chmod(0o660)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        lease_marker(path).unlink(missing_ok=True)
        raise


def copy_file(source: Path, original_name: str | None = None) -> Path:
    path = allocate_path(original_name or source.name)
    try:
        shutil.copyfile(source, path)
        path.chmod(0o660)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        lease_marker(path).unlink(missing_ok=True)
        raise


def failed_marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.failed.json")


def lease_marker(path: Path) -> Path:
    return path.with_name(f".{path.name}.lease.json")


def _create_active_lease(path: Path) -> None:
    marker = lease_marker(path)
    payload = {
        "media": path.name,
        "status": "active",
        "created_at": _now().isoformat(),
    }
    descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.write("\n")
        marker.chmod(0o660)
    except Exception:
        marker.unlink(missing_ok=True)
        raise


def mark_failed(path: Path, error: BaseException | str | None = None) -> None:
    if not path.exists():
        return
    marker = failed_marker(path)
    payload = {
        "media": path.name,
        "failed_at": _now().isoformat(),
        "error": type(error).__name__ if isinstance(error, BaseException) else str(error or ""),
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    marker.chmod(0o660)
    lease_marker(path).unlink(missing_ok=True)


def cleanup_success(*paths: Path | None) -> None:
    for item in paths:
        if item is None:
            continue
        path = Path(item)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        failed_marker(path).unlink(missing_ok=True)
        lease_marker(path).unlink(missing_ok=True)


@contextmanager
def retained_on_failure(path: Path) -> Iterator[Path]:
    try:
        yield path
    except BaseException as exc:
        mark_failed(path, exc)
        raise
    else:
        cleanup_success(path)


def allocate_work_directory(label: str = "work", *, root: Path | None = None) -> Path:
    safe_label = sanitize_original_name(label, fallback="work").replace(".", "_")
    directory = daily_directory(root=root) / f"{uuid4().hex}-{safe_label}"
    directory.mkdir(mode=0o770)
    try:
        _create_active_lease(directory)
    except Exception:
        directory.rmdir()
        raise
    return directory


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        return False
    return True


def cleanup_expired(
    *,
    root: Path = TEMP_DATA_ROOT,
    retention_seconds: int = FAILURE_RETENTION_SECONDS,
    now_timestamp: float | None = None,
) -> dict[str, int]:
    resolved_root = root.resolve(strict=True)
    if resolved_root == Path("/") or resolved_root == Path("/home/donxu"):
        raise ValueError("refusing to clean an unsafe root")
    cutoff = (now_timestamp if now_timestamp is not None else _now().timestamp()) - retention_seconds
    removed_files = 0
    removed_directories = 0
    for date_directory in list(resolved_root.iterdir()):
        if not date_directory.is_dir() or not re.fullmatch(r"\d{8}", date_directory.name):
            continue
        markers = {
            marker.name[len(".") : -len(".failed.json")]: marker
            for marker in date_directory.glob(".*.failed.json")
        }
        leases = {
            marker.name[len(".") : -len(".lease.json")]: marker
            for marker in date_directory.glob(".*.lease.json")
        }
        for child in list(date_directory.iterdir()):
            if not _inside_root(child, resolved_root):
                continue
            if not child.exists():
                continue
            if child.name.startswith(".") and child.name.endswith(".failed.json"):
                media_name = child.name[1 : -len(".failed.json")]
                media = date_directory / media_name
                if child.stat().st_mtime <= cutoff:
                    if media.is_dir():
                        shutil.rmtree(media, ignore_errors=True)
                        removed_directories += 1
                    elif media.exists():
                        media.unlink(missing_ok=True)
                        removed_files += 1
                    child.unlink(missing_ok=True)
                    removed_files += 1
                    lease_marker(media).unlink(missing_ok=True)
                continue
            if child.name.startswith(".") and child.name.endswith(".lease.json"):
                media_name = child.name[1 : -len(".lease.json")]
                media = date_directory / media_name
                if media_name in markers:
                    continue
                if child.stat().st_mtime <= cutoff:
                    if media.is_dir():
                        shutil.rmtree(media, ignore_errors=True)
                        removed_directories += 1
                    elif media.exists():
                        media.unlink(missing_ok=True)
                        removed_files += 1
                    child.unlink(missing_ok=True)
                    removed_files += 1
                continue
            marker = markers.get(child.name)
            lease = leases.get(child.name)
            if (marker is not None and marker.exists()) or (
                lease is not None and lease.exists()
            ):
                continue
            if child.stat().st_mtime > cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed_directories += 1
            else:
                child.unlink(missing_ok=True)
                removed_files += 1
        try:
            date_directory.rmdir()
            removed_directories += 1
        except OSError:
            pass
    return {"removed_files": removed_files, "removed_directories": removed_directories}


def _command_allocate(args: argparse.Namespace) -> int:
    print(allocate_path(args.name, suffix=args.suffix))
    return 0


def _command_success(args: argparse.Namespace) -> int:
    cleanup_success(Path(args.path))
    return 0


def _command_fail(args: argparse.Namespace) -> int:
    mark_failed(Path(args.path), args.error)
    return 0


def _command_cleanup(args: argparse.Namespace) -> int:
    print(json.dumps(cleanup_expired(retention_seconds=args.retention_seconds)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Allocate and clean AI Centre media temp files")
    commands = parser.add_subparsers(dest="command", required=True)
    allocate = commands.add_parser("allocate")
    allocate.add_argument("--name", required=True)
    allocate.add_argument("--suffix")
    allocate.set_defaults(handler=_command_allocate)
    success = commands.add_parser("success")
    success.add_argument("path")
    success.set_defaults(handler=_command_success)
    fail = commands.add_parser("fail")
    fail.add_argument("path")
    fail.add_argument("--error", default="manual failure")
    fail.set_defaults(handler=_command_fail)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--retention-seconds", type=int, default=FAILURE_RETENTION_SECONDS)
    cleanup.set_defaults(handler=_command_cleanup)
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
