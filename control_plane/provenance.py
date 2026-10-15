"""AI 生成内容的**溯源标识**（隐式）与**显式标识**。

算法备案那条要求两件事：

1. **溯源标识（隐式）**——把 AIGC 元数据写进文件容器本身。不改画面、不改听感，
   但能被读回来核验。这个模块的实现方式由三条实测约束逼出来的，不是任意选择：

   - **服务器那份 ffmpeg 没有 `drawtext`、没有 libfreetype**（johnvansickle 静态构建
     v7.0.2，`.venv-control`/`.venv-tts`/`.venv-depth` 三份都是），而且**服务器上一个
     中文字体都没有**（只有 DejaVu，没有 CJK 字形）。所以「画面上叠中文」不能走
     `drawtext`，只能是**预渲染好的 PNG + `overlay`**。
   - **ffmpeg 只对 mp4/mov 复用器认 `-metadata`**，`image2`（png/jpeg）与 `mp3`
     都不认。所以 png/jpeg/mp3 的元数据要**自己写容器**。
   - **控制面 venv 没有 Pillow、没有 mutagen**（只有 ffmpeg 与 oss2），
     所以容器读写全部是**纯标准库**，不引新依赖。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imageio_ffmpeg


# 元数据用的关键字 / 描述子。读回时按它找，不靠位置猜。
PNG_KEYWORD = "AIGC"
ID3_DESCRIPTION = "AIGC"


def ffmpeg_bin() -> str:
    """优先用系统 ffmpeg，没有就用 imageio-ffmpeg 带的那份。

    与 generation_tasks._prepare_video_for_upload 同一个取法，保持全仓一致。
    """
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("ffmpeg is required for provenance marking") from exc


# ---------------------------------------------------------------- 容器：PNG

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_iter_chunks(data: bytes):
    """产出 (kind, payload, 起始偏移, 总长度)。不认识的块也照样产出，调用方自己挑。"""
    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        total = 12 + length
        if offset + total > len(data):
            break
        yield kind, payload, offset, total
        offset += total


def write_png_text(path: Path, text: str, keyword: str = PNG_KEYWORD) -> None:
    """把 text 作为一个 `iTXt` 块写进 PNG。

    用 `iTXt` 而不是 `tEXt`：`tEXt` 只允许 Latin-1，我们的 JSON 里带着中文，
    塞进去会坏码。`iTXt` 是 UTF-8。

    **先删掉同关键字的旧块再写**，不然反复标注会越堆越多（读回时拿到哪一条全看运气）。
    """
    data = path.read_bytes()
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError(f"{path.name} 不是 PNG")

    chunk = _png_chunk(
        b"iTXt",
        keyword.encode("latin-1")
        + b"\x00"          # 关键字结束
        + b"\x00"          # 压缩标志：0 = 不压缩
        + b"\x00"          # 压缩方法
        + b"\x00"          # 语言标签为空
        + b"\x00"          # 翻译关键字为空
        + text.encode("utf-8"),
    )

    output = bytearray(_PNG_SIGNATURE)
    replaced = False
    for kind, payload, _offset, _total in _png_iter_chunks(data):
        if kind == b"iTXt" and payload.split(b"\x00", 1)[0] == keyword.encode("latin-1"):
            if not replaced:
                output += chunk
                replaced = True
            continue
        output += _png_chunk(kind, payload)
    if not replaced:
        # 插在 IEND 之前
        iend = output.rfind(_png_chunk(b"IEND", b""))
        if iend < 0:
            output += chunk + _png_chunk(b"IEND", b"")
        else:
            output[iend:iend] = chunk
    path.write_bytes(bytes(output))


def read_png_text(path: Path, keyword: str = PNG_KEYWORD) -> str | None:
    data = path.read_bytes()
    if not data.startswith(_PNG_SIGNATURE):
        return None
    for kind, payload, _offset, _total in _png_iter_chunks(data):
        if kind != b"iTXt":
            continue
        parts = payload.split(b"\x00", 5)
        if len(parts) < 6 or parts[0] != keyword.encode("latin-1"):
            continue
        # parts = [keyword, compression_flag, compression_method, language, translated, text]
        if parts[1] == b"\x01":
            return zlib.decompress(parts[5]).decode("utf-8", "replace")
        return parts[5].decode("utf-8", "replace")
    return None


# ---------------------------------------------------------------- 容器：JPEG


def write_jpeg_comment(path: Path, text: str) -> None:
    """把 text 作为一个 `COM`（0xFFFE）段插在 SOI 之后。

    **先剥掉已有的 COM 段**再写，理由同 PNG：反复标注不该越堆越多。
    """
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"{path.name} 不是 JPEG")

    payload = text.encode("utf-8")
    if len(payload) + 2 > 0xFFFF:
        raise ValueError("provenance payload is too large for a JPEG COM segment")
    segment = b"\xff\xfe" + struct.pack(">H", len(payload) + 2) + payload

    output = bytearray(b"\xff\xd8")
    offset = 2
    inserted = False
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            break
        marker = data[offset + 1]
        if marker == 0xFE:                      # 旧的 COM，丢掉
            (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
            offset += 2 + length
            continue
        if marker in (0xDA, 0xD9):              # SOS / EOI，再往后是压缩数据
            break
        (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
        output += data[offset : offset + 2 + length]
        offset += 2 + length
    output += segment
    output += data[offset:]
    path.write_bytes(bytes(output))


def read_jpeg_comment(path: Path) -> str | None:
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker in (0xDA, 0xD9):
            return None
        (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
        if marker == 0xFE:
            return data[offset + 4 : offset + 2 + length].decode("utf-8", "replace")
        offset += 2 + length
    return None


# ---------------------------------------------------------------- 容器：MP3


def _syncsafe(value: int) -> bytes:
    return bytes(
        ((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F)
    )


def _unsyncsafe(data: bytes) -> int:
    return (
        (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]
    )


def _strip_id3v2(data: bytes) -> bytes:
    if not data.startswith(b"ID3") or len(data) < 10:
        return data
    size = _unsyncsafe(data[6:10])
    flags = data[5]
    total = 10 + size
    if flags & 0x10:                            # 有 footer，再加 10 字节
        total += 10
    return data[total:]


def write_mp3_tag(path: Path, text: str, description: str = ID3_DESCRIPTION) -> None:
    """写一个 ID3v2.3 的 `TXXX` 帧。

    **整条重写、先剥旧的**：ID3v2 允许前面挂多个标签，但那样既有实现读到的往往是
    第一个（可能是旧的、甚至是别人的），我们不冒这个险 —— 保证文件头部只有一条我们的。
    """
    data = path.read_bytes()
    audio = _strip_id3v2(data)

    payload = b"\x03"                              # 编码：3 = UTF-8
    payload += description.encode("utf-8") + b"\x00"
    payload += text.encode("utf-8")
    frame = b"TXXX" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload

    tag = b"ID3\x03\x00\x00" + _syncsafe(len(frame)) + frame
    path.write_bytes(tag + audio)


def read_mp3_tag(path: Path, description: str = ID3_DESCRIPTION) -> str | None:
    data = path.read_bytes()
    if not data.startswith(b"ID3") or len(data) < 10:
        return None
    major = data[3]
    size = _unsyncsafe(data[6:10])
    body = data[10 : 10 + size]
    offset = 0
    while offset + 10 <= len(body):
        frame_id = body[offset : offset + 4]
        if not frame_id.strip(b"\x00"):
            break
        if major >= 4:
            frame_size = _unsyncsafe(body[offset + 4 : offset + 8])
        else:
            (frame_size,) = struct.unpack(">I", body[offset + 4 : offset + 8])
        payload = body[offset + 10 : offset + 10 + frame_size]
        if frame_id == b"TXXX" and payload:
            parts = payload[1:].split(b"\x00", 1)
            if len(parts) == 2 and parts[0].decode("utf-8", "replace") == description:
                return parts[1].decode("utf-8", "replace")
        offset += 10 + frame_size
    return None


# ---------------------------------------------------------------- 容器：MP4（走 ffmpeg）

_FFMETADATA_PREFIX = ";FFMETADATA1"


def write_mp4_metadata(path: Path, text: str, work: Path | None = None) -> Path:
    """用 ffmpeg 把 text 写进 mp4 的 `comment`。

    `-c copy`：只换容器、不重编码，无损且快。返回写好的文件（可能是新路径 ——
    ffmpeg 不能就地改写）。
    """
    target = (work or path.with_name(path.stem + "-tagged" + path.suffix))
    completed = subprocess.run(
        [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path),
            "-c", "copy", "-map_metadata", "0",
            "-metadata", f"comment={text}",
            "-movflags", "+faststart",
            str(target),
        ],
        capture_output=True,
        timeout=1800,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"写 mp4 元数据失败: {completed.stderr.decode('utf-8', 'replace')[-300:]}"
        )
    return target


def read_mp4_metadata(path: Path) -> str | None:
    """用 `ffmpeg -f ffmetadata -` 把元数据 dump 成文本再解析。

    **不用 ffprobe**：imageio-ffmpeg 只带 ffmpeg，不带 ffprobe，用 ffprobe 会在
    服务器上直接找不到可执行文件。
    """
    completed = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-f", "ffmetadata", "-"],
        capture_output=True,
        timeout=600,
    )
    if completed.returncode != 0:
        return None
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("comment="):
            return line[len("comment=") :]
    return None


# ---------------------------------------------------------------- 分派


def detect_kind(path: Path) -> str | None:
    """按文件头判类型，不看扩展名（上游给的文件名经常对不上内容）。"""
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return None
    if head.startswith(_PNG_SIGNATURE):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    return None


def read_provenance(path: Path) -> dict[str, Any] | None:
    """把写进去的东西读回来。读不到返回 None —— 不抛错。

    上传接口要能对着**任意文件**回答「里面有没有标识」，所以「没有」是正常结果，
    不是异常。
    """
    kind = detect_kind(path)
    if kind == "png":
        text = read_png_text(path)
        where = "PNG iTXt"
    elif kind == "jpeg":
        text = read_jpeg_comment(path)
        where = "JPEG COM"
    elif kind == "mp3":
        text = read_mp3_tag(path)
        where = "ID3v2 TXXX"
    elif kind == "mp4":
        text = read_mp4_metadata(path)
        where = "MP4 comment"
    else:
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    return {
        "container": kind,
        "where": where,
        "raw": text,
        "payload": payload if isinstance(payload, dict) else None,
        # 解不出 JSON 也算读到了东西（可能是别家写的 comment），如实报告而不是说没有。
        "parseable": isinstance(payload, dict),
    }


# ---------------------------------------------------------------- AIGC 载荷


def _md5(*parts: str) -> str:
    digest = hashlib.md5()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
    return digest.hexdigest()


def build_aigc_payload(
    *,
    job_id: str,
    external_ref: str | None,
    filename: str,
    settings: Any,
) -> tuple[dict[str, Any], list[str]]:
    """按备案规范拼一份 AIGC 载荷，返回 (载荷, 缺什么)。

    `Label` = "1" 表示「属于生成合成内容」。其余字段的语义照规范：

    | 字段 | 规范要求 | 我们放什么 |
    |---|---|---|
    | ContentProducer | 生成合成服务提供者的名称或编码 | 配置项，可留空 |
    | ProduceID | 算法服务版本号 + 生成内容 ID | `md5(算法版本 + job_id)` |
    | ReservedCode1 | 文件名 + 文件生成时间 | `md5(文件名 + 时间)` |
    | ContentPropagator | 企业社会信用代码 | 配置项，可留空 |
    | PropagateID | 时间戳 + 用户 ID + 手机号 | **替代项**，见下 |
    | ReservedCode2 | 加密后的信用代码 | `hash:md5(信用代码)` |

    **`PropagateID` 是替代实现，不是规范实现。** 规范要的是「时间戳+用户ID+手机号」，
    而中台压根不知道调用方的用户是谁 —— 手上只有 `external_ref`。这里用
    `时间戳 + external_ref + 渠道` 代替。等中台有了用户体系（或调用方开始传身份字段）
    再换成真的。**不假装这里是完整的**。

    返回的第二个值是「哪些字段因为没配置而缺失」，调用方据此如实报告，
    而不是静默写一份半截载荷。
    """
    stamp = datetime.now(timezone.utc)
    missing: list[str] = []

    producer = str(getattr(settings, "aigc_content_producer", "") or "").strip()
    propagator = str(getattr(settings, "aigc_content_propagator", "") or "").strip()
    version = str(getattr(settings, "aigc_algorithm_version", "") or "").strip()
    if not producer:
        missing.append("ContentProducer")
    if not propagator:
        missing.append("ContentPropagator")

    return (
        {
            "AIGC": {
                "Label": "1",
                "ContentProducer": producer,
                "ProduceID": _md5(version, job_id),
                "ReservedCode1": _md5(filename, stamp.isoformat()),
                # 替代项：没有用户 ID 与手机号，用 external_ref 顶上（见 docstring）。
                "PropagateID": _md5(str(stamp.timestamp()), external_ref or job_id),
                "ContentPropagator": propagator,
                "ReservedCode2": "hash:" + _md5(propagator) if propagator else "",
            }
        },
        missing,
    )


# ---------------------------------------------------------------- 显式角标

LABEL_ASSET = Path(__file__).with_name("assets") / "aigc-label.png"


def _probe_size(path: Path) -> tuple[int, int]:
    """取画面宽高。

    **不用 ffprobe**：imageio-ffmpeg 只带 ffmpeg，服务器上没有 ffprobe。
    ffmpeg 把流信息打在 stderr 上（`-loglevel error` 会把它吞掉，所以这里故意不设级别），
    从里面取第一个 `NNNxNNN`。
    """
    completed = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        timeout=120,
    )
    match = re.search(rb"(\d{2,5})x(\d{2,5})", completed.stderr)
    if not match:
        raise RuntimeError(f"读不出 {path.name} 的画面尺寸")
    return int(match.group(1)), int(match.group(2))


def overlay_label(src: Path, dst: Path, kind: str | None = None) -> None:
    """把预渲染的「AI生成」角标叠到左上角。

    为什么是预渲染：服务器 ffmpeg **没有 drawtext/libfreetype**，也没有任何中文字体，
    用 `drawtext` 渲染中文只会失败或出方框。所以文案在开发机上烤成 PNG，
    运行时只做 `overlay`。

    宽度取画面宽的 12%，**上限 22%** —— 小图上不设上限的话角标会喧宾夺主。

    图片与视频共用同一段滤镜，只有编码参数不同；按**探测出来的类型**分支，
    不用扩展名（上游给的文件名经常对不上内容）。
    """
    if not LABEL_ASSET.is_file():
        raise RuntimeError(f"角标素材缺失：{LABEL_ASSET}")
    kind = kind or detect_kind(src)
    width, _height = _probe_size(src)
    label_width = max(48, min(int(width * 0.12), int(width * 0.22)))
    margin = max(6, int(width * 0.02))

    command = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-i", str(LABEL_ASSET),
        "-filter_complex",
        f"[1:v]scale={label_width}:-1[lg];[0:v][lg]overlay={margin}:{margin}:format=auto",
    ]
    if kind == "mp4":
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "copy", "-movflags", "+faststart"]
    else:
        # 静帧：交给 ffmpeg 按输出扩展名挑编码器（png / mjpeg）。
        command += ["-frames:v", "1"]
    command.append(str(dst))

    completed = subprocess.run(command, capture_output=True, timeout=3600)
    if completed.returncode != 0:
        raise RuntimeError(
            f"叠加角标失败: {completed.stderr.decode('utf-8', 'replace')[-300:]}"
        )


# ---------------------------------------------------------------- 对外的总入口


def apply_provenance(
    path: Path,
    *,
    job_id: str,
    external_ref: str | None,
    explicit_label: bool,
    settings: Any,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """就地给一个产物文件打上标识。返回一份「做了什么 / 没做什么」的报告。

    **改的是这个文件本身**（调用方拿到的是本地临时文件，打完就该上传了），
    不额外产生副本；只有叠角标那一步需要中间文件，用 work_dir 放。
    """
    kind = detect_kind(path)
    report: dict[str, Any] = {
        "container": kind,
        "implicit": False,
        "explicit": False,
        "missing_fields": [],
        "note": "",
    }
    if kind is None:
        report["note"] = f"unknown container, skipped ({path.suffix or 'no suffix'})"
        return report

    payload, missing = build_aigc_payload(
        job_id=job_id, external_ref=external_ref, filename=path.name, settings=settings
    )
    report["missing_fields"] = missing
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    written = path
    if explicit_label and kind in {"png", "jpeg", "mp4"}:
        tmp = (work_dir or path.parent) / (path.stem + "-labeled" + path.suffix)
        overlay_label(path, tmp)
        written = tmp
        report["explicit"] = True

    if kind == "png":
        write_png_text(written, text)
    elif kind == "jpeg":
        write_jpeg_comment(written, text)
    elif kind == "mp3":
        write_mp3_tag(written, text)
    elif kind == "mp4":
        tagged = write_mp4_metadata(written, text)
        if written != path:
            written.unlink(missing_ok=True)
        tagged.replace(path)
        written = path
    report["implicit"] = True

    if written != path:
        written.replace(path)
    if missing:
        report["note"] = "企业信息未配置：" + "、".join(missing)
    return report
