from __future__ import annotations

import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg

from .celery_app import celery_app
from .concurrency import job_slot, segment_limit, slot_wait_reporter
from .config import get_settings
from .media_fetch import VIDEO_MEDIA, download_public_media


# video_field 随节点类型而变：flashvsr 的视频节点是 LoadVideo（字段 file），
# 另两个是 VHS_LoadVideo（字段 video）。节点号以 RunningHub 应用详情页的
# nodeInfoList 为准，改应用后可能失效。
#
# instance_type 同理按 provider 定：flashvsr 两个工作流在小卡（default）上跑真实素材
# 会稳定「工作流运行失败」（实测同一段 11.8s@1920：default 约 210s 必挂，plus 302s 成功），
# 所以固定 plus。seedvr2 负载轻，整片 22s@最短边1080 在 default 上也能过（916s）。
PROVIDERS: dict[str, dict[str, str]] = {
    "flashvsr": {"app_id": "1996062530516795394", "video_node": "27", "video_field": "file", "size_node": "16",
                 "instance_type": "plus"},
    "flashvsr_v2": {"app_id": "1983119055743819777", "video_node": "24", "video_field": "video", "size_node": "16",
                    "instance_type": "plus"},
    "seedvr2": {"app_id": "1990029249488801793", "video_node": "16", "video_field": "video", "size_node": "71",
                "instance_type": "default"},
}
TERMINAL_SUCCESS = {"SUCCESS", "SUCCEEDED", "COMPLETED", "COMPLETE", "FINISHED"}
TERMINAL_FAILURE = {"FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED"}


class ProviderFailure(RuntimeError):
    pass


def provider_order(requested: str, configured: str) -> list[str]:
    if requested != "auto":
        return [requested]
    result = [item.strip().lower() for item in configured.split(",") if item.strip().lower() in PROVIDERS]
    return list(dict.fromkeys(result)) or list(PROVIDERS)


def build_payload(provider: str, source_uri: str, max_resolution: int) -> dict[str, Any]:
    spec = PROVIDERS[provider]
    size_description = "设置最短边" if provider == "seedvr2" else "最大分辨率设置"
    # 超过 1920 一律上 plus；此外 provider 自己也能要求（见 PROVIDERS 上的注释）。
    instance_type = "plus" if max_resolution > 1920 else spec["instance_type"]
    return {
        "nodeInfoList": [
            {"nodeId": spec["video_node"], "fieldName": spec["video_field"], "fieldValue": source_uri,
             "description": "上传视频"},
            {"nodeId": spec["size_node"], "fieldName": "value", "fieldValue": str(max_resolution),
             "description": size_description},
        ],
        "instanceType": instance_type,
        "usePersonalQueue": "false",
    }


def _find_result_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("https://"):
        return value
    if isinstance(value, dict):
        preferred = ("url", "fileUrl", "file_url", "videoUrl", "video_url")
        for key in preferred:
            found = _find_result_url(value.get(key))
            if found:
                return found
        for item in value.values():
            found = _find_result_url(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_result_url(item)
            if found:
                return found
    return None


def run_provider(
    provider: str,
    request_data: dict[str, Any],
    update_progress,
) -> tuple[str, str]:
    settings = get_settings()
    token = settings.runninghub_api_token.get_secret_value() if settings.runninghub_api_token else ""
    if not token:
        raise ProviderFailure("RunningHub API token is not configured")
    base = settings.runninghub_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    spec = PROVIDERS[provider]
    timeout = httpx.Timeout(settings.video_upscale_request_timeout_seconds, connect=20)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=False) as client:
        response = client.post(
            f"{base}/openapi/v2/run/ai-app/{spec['app_id']}",
            json=build_payload(provider, request_data["source_uri"], request_data["max_resolution"]),
        )
        response.raise_for_status()
        body = response.json()
        task_id = str(body.get("taskId") or "")
        if not task_id:
            raise ProviderFailure("provider did not return taskId")
        deadline = time.monotonic() + settings.video_upscale_provider_timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(settings.video_upscale_poll_seconds)
            query = client.post(f"{base}/openapi/v2/query", json={"taskId": task_id})
            query.raise_for_status()
            payload = query.json()
            state = str(payload.get("status") or "").upper()
            if state in TERMINAL_FAILURE:
                reason = payload.get("errorMessage") or payload.get("failedReason") or state
                raise ProviderFailure(f"provider terminal failure: {str(reason)[:240]}")
            result_url = _find_result_url(payload.get("results"))
            if state in TERMINAL_SUCCESS and result_url:
                return result_url, task_id
            progress = payload.get("progress")
            update_progress(int(progress) if isinstance(progress, (int, float)) else 50)
        raise ProviderFailure("provider query timed out")


def _run_ffmpeg(arguments: list[str]) -> None:
    settings = get_settings()
    ffmpeg_bin = (
        imageio_ffmpeg.get_ffmpeg_exe()
        if settings.video_upscale_ffmpeg_bin == "auto"
        else settings.video_upscale_ffmpeg_bin
    )
    try:
        subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=settings.video_upscale_ffmpeg_timeout_seconds,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        raise RuntimeError(f"video preparation failed: {detail[-400:]}") from exc


# 分段的两条硬约束，都来自实测（见 memory：闹海 1080p 成片的两个自带缺陷）：
#
# 1. 首帧崩坏：上游是分块推理，每一段的头几帧没有历史可依，出来的第一帧必崩
#    （成片里每段衔接处的首帧软 ~30%）。所以每段请求往前多带 head 帧真实内容，
#    真正要的那几帧就不再是模型的「第一帧」；合并时把这几帧连同崩掉的头部一起丢掉。
#    首段前面没有内容可借，就用自己的首帧冻结出 head 帧当上下文 —— 同样是丢弃，
#    但成片的第一帧因此也落在「预热之后」，不是模型的冷启动帧。
#
# 2. 每段停滞：上游普遍会丢掉输出末尾若干帧（video2x 稳定丢 2 帧、Topaz Starlight
#    Mini 丢 8~16 帧，我们实测约 8~10 帧）。旧代码用 tpad=stop_mode=clone 拿上一帧
#    补齐，于是每 11.8s 冻结一次（成片实测 32 处、每处 8~13 帧）。现在请求里带
#    tail 帧余量，只取余量之前的帧 —— 上游丢的那几帧落在余量里，正片一帧不少、
#    一帧不重，拼出来就是源片。
#
# 两者都是从「源片自己的帧」里取的，所以既不引入复制帧也不引入跳帧。
PROVIDER_FPS_TOLERANCE = 0.002


def _frame_count(path: Path) -> int:
    return int(imageio_ffmpeg.count_frames_and_secs(str(path))[0])


def _seconds(value: float) -> str:
    return f"{value:.6f}"


def _rate(fps: float) -> str:
    """给编码器一个明确的输出帧率。

    只写 -fps_mode passthrough 的话，mp4 的**最后一个样本时长会是 0**；拼接器
    按「文件声明的时长」推下一段的偏移，于是每段接缝都出现一对零间隔帧 ——
    成片每 11.8s 少一帧的显示时间，整条时间轴比源片短。带上 -r 后每个样本
    都是 1/fps，帧数不变（滤镜已经把时间戳钉在 N/fps 上了）。
    """
    return f"{fps:g}"


@dataclass(frozen=True)
class SegmentPlan:
    """一段的帧账。全部按整数帧算：秒只用来决定切点，绝不用来对齐。"""

    index: int
    start_frame: int          # 上传件第一帧在源片里的帧号（首段的段首上下文是冻结帧，所以是负的）
    body_start: int           # 正片第一帧在源片里的帧号
    body_frames: int
    head_frames: int          # 段首上下文：借上一段的真实尾帧（首段借不到，用自己的首帧冻结）
    tail_real_frames: int     # 段尾余量里的真实帧：借下一段的真实头帧
    tail_clone_frames: int    # 段尾余量里凑数的冻结帧（只有末段才借不到）

    @property
    def tail_frames(self) -> int:
        return self.tail_real_frames + self.tail_clone_frames

    @property
    def upload_frames(self) -> int:
        return self.head_frames + self.body_frames + self.tail_frames


@dataclass(frozen=True)
class Segment:
    """切好拼好、可以直接上传给上游的一段。"""

    plan: SegmentPlan
    upload: Path
    upload_frames: int        # 实测：上传件帧数
    keep_from: int            # 实测：正片在上传件里的第一帧
    keep_frames: int          # 正片帧数

    @property
    def index(self) -> int:
        return self.plan.index

    @property
    def min_frames(self) -> int:
        """上游至少得还回这么多帧，正片才可能一帧不缺。"""
        return self.keep_from + self.keep_frames


def plan_segments(body_frames: list[int], head_frames: int, tail_frames: int,
                  upload_ceiling_frames: int) -> list[SegmentPlan]:
    """按实测的每段帧数排帧账。

    相邻两段的正片严格首尾相接：既不重叠也不留缝，所以拼出来就是源片本身。
    段首上下文借上一段的真实尾帧（首段借用自己首帧的冻结帧），段尾余量先借下一段的
    真实头帧，不够的部分用冻结帧凑（末段没有下一段，所以末段的余量全是冻结帧）。
    冻结帧只存在于上传件里，永远不进正片。
    """
    plans: list[SegmentPlan] = []
    body_start = 0
    for position, body in enumerate(body_frames):
        previous = body_frames[position - 1] if position else 0
        following = body_frames[position + 1] if position + 1 < len(body_frames) else 0
        # 首段前面没有内容可借，就用自己的首帧冻结出段首上下文：它只给上游预热，
        # 合并时整段丢弃，所以成片的第一帧也是「预热过」的那一帧，不是模型的冷启动帧。
        head = min(head_frames, previous) if position else head_frames
        # 平台的时长上限是按秒卡的，而切点跟着关键帧走：实测会有段落多出一帧
        # （354 帧的上传件变成 355 帧 = 11.8333s > 11.8s）。多出来的从段尾余量里扣，
        # 正片一帧不动 —— 余量本来就是抗上游丢帧的缓冲，短一两帧不影响。
        margin = min(tail_frames, max(0, upload_ceiling_frames - head - body))
        tail_real = min(margin, following)
        plans.append(SegmentPlan(
            index=position + 1, start_frame=body_start - head, body_start=body_start,
            body_frames=body, head_frames=head,
            tail_real_frames=tail_real, tail_clone_frames=margin - tail_real,
        ))
        body_start += body
    return plans


def _slice_segment(source: Path, filters: str, frames: int, target: Path, fps: float) -> int:
    """从一段里裁出 frames 帧（按帧号裁，不按秒），返回实测帧数。"""
    _run_ffmpeg([
        "-i", str(source), "-an",
        "-vf", f"{filters},setpts=PTS-STARTPTS",
        "-frames:v", str(frames), "-r", _rate(fps),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        str(target),
    ])
    return _frame_count(target)


def split_video(
    source: Path, directory: Path, segment_seconds: float, fps: float,
    head_frames: int, tail_frames: int,
) -> list[Segment]:
    """把源片切成「带上下文的上传件」。

    一次过切出正片段（帧对齐、只在切点放关键帧），再给每段拼上首尾上下文；
    拼接走流拷贝，所以正片在本地只过一遍编码。所有帧数都从切出来的文件里量，
    不拿秒数估。
    """
    directory.mkdir(parents=True, exist_ok=True)
    parts = directory / "parts"
    parts.mkdir(exist_ok=True)
    for stale in parts.glob("*.mp4"):
        stale.unlink()
    # 上游上限算的是上传件的时长，所以要先把首尾上下文的时长让出来。
    ceiling_frames = max(1, round(segment_seconds * fps))
    body_frames = max(1, ceiling_frames - head_frames - tail_frames)
    body_seconds = body_frames / fps
    _run_ffmpeg([
        "-i", str(source), "-map", "0:v:0", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-sc_threshold", "0", "-g", str(body_frames), "-keyint_min", str(body_frames),
        "-force_key_frames", f"expr:gte(t,n_forced*{_seconds(body_seconds)})",
        "-f", "segment", "-segment_time", _seconds(body_seconds),
        "-segment_time_delta", _seconds(min(0.5, body_seconds / 2)),
        "-reset_timestamps", "1", str(parts / "body-%05d.mp4"),
    ])
    bodies = sorted(parts.glob("body-*.mp4"))
    if not bodies:
        raise RuntimeError("video splitter produced no segments")
    plans = plan_segments([_frame_count(path) for path in bodies], head_frames, tail_frames, ceiling_frames)
    segments: list[Segment] = []
    for position, (plan, body) in enumerate(zip(plans, bodies, strict=True)):
        pieces: list[Path] = []
        if plan.head_frames:
            head = parts / f"head-{plan.index:05d}.mp4"
            # 有上一段就借它的真实尾帧，首段没有上一段，拿自己的首帧冻结凑。
            # tpad 的 start 只收非负时长（start=-1 会被拒），所以按帧率算一个够用的值：
            # 给多了被 -frames:v 截掉，给少了用真实帧补上，两种都不影响正片对齐
            # —— 段首上下文永远是 head_frames 帧，取正片时整段跳过。
            trim = (f"trim=start_frame={plans[position - 1].body_frames - plan.head_frames}" if position
                    else f"tpad=start_mode=clone:start_duration={_seconds(2 * plan.head_frames / fps)}")
            got = _slice_segment(bodies[position - 1] if position else body, trim, plan.head_frames, head, fps)
            if got != plan.head_frames:
                raise RuntimeError(f"segment {plan.index}: head context sliced {got} frames, expected {plan.head_frames}")
            pieces.append(head)
        pieces.append(body)
        if plan.tail_frames:
            tail = parts / f"tail-{plan.index:05d}.mp4"
            # 有下一段就借真实头帧，末段只能拿自己的末帧冻结凑数。
            origin = bodies[position + 1] if plan.tail_real_frames else body
            trim = (f"trim=end_frame={plan.tail_real_frames}" if plan.tail_real_frames
                    else f"trim=start_frame={max(0, plan.body_frames - 1)}")
            got = _slice_segment(origin, f"{trim},tpad=stop_mode=clone:stop=-1", plan.tail_frames, tail, fps)
            if got != plan.tail_frames:
                raise RuntimeError(f"segment {plan.index}: tail margin sliced {got} frames, expected {plan.tail_frames}")
            pieces.append(tail)
        if len(pieces) == 1:
            upload = pieces[0]
        else:
            concat_file = parts / f"upload-{plan.index:05d}.txt"
            concat_file.write_text("".join(f"file '{path.as_posix()}'\n" for path in pieces), encoding="utf-8")
            upload = directory / f"segment-{plan.index:05d}.mp4"
            _run_ffmpeg([
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c", "copy", "-movflags", "+faststart", str(upload),
            ])
        measured = _frame_count(upload)
        if measured != plan.upload_frames:
            raise RuntimeError(f"segment {plan.index}: upload has {measured} frames, expected {plan.upload_frames}")
        segments.append(Segment(
            plan=plan, upload=upload, upload_frames=measured,
            keep_from=plan.head_frames, keep_frames=plan.body_frames,
        ))
    return segments



def _upload_file(path: Path, job_id: str, stage: str, filename: str) -> str:
    settings = get_settings()
    if not settings.kernel_upload_url or not settings.kernel_api_token:
        raise RuntimeError("kernel upload is not configured")
    with path.open("rb") as stream:
        response = httpx.post(
            settings.kernel_upload_url,
            headers={"Authorization": f"Bearer {settings.kernel_api_token}"},
            data={"external_ref": f"{job_id}:{stage}", "stage": stage, "actor": "video-upscale-worker"},
            files={"file": (filename, stream, "video/mp4")},
            timeout=httpx.Timeout(settings.video_upscale_upload_timeout_seconds, connect=30),
        )
    response.raise_for_status()
    url = str(response.json().get("uri") or "")
    if not url.startswith("https://"):
        raise RuntimeError("kernel upload did not return an HTTPS URL")
    return url


def _process_segment(
    segment: Segment,
    segment_url: str,
    request_data: dict[str, Any],
    configured_order: str,
    max_attempts: int,
    output_dir: Path,
    fps: float,
) -> dict[str, Any]:
    requested = str(request_data.get("provider", "auto"))
    providers = provider_order(requested, configured_order)
    if requested != "auto":
        providers = providers * max_attempts
    else:
        providers = (providers * max_attempts)[:max_attempts]
    attempts: list[dict[str, str]] = []
    for attempt_number, provider in enumerate(providers, start=1):
        try:
            result_url, _ = run_provider(
                provider,
                {**request_data, "source_uri": segment_url},
                lambda _progress: None,
            )
            downloaded = download_public_media(
                result_url, output_dir, f"upscaled-{segment.index:04d}", VIDEO_MEDIA,
                get_settings().video_upscale_max_download_bytes,
                get_settings().video_upscale_provider_timeout_seconds,
            )
            # 上游结果必须「够长且帧率相符」，否则宁可换一家重试：
            # 短了只能靠克隆补（就是那个停滞），帧率低了只能靠复制帧补（就是那个 24→30）。
            # 缺的帧只有上游能补出来，本地补出来的必然是假的。
            provider_frames, provider_seconds = imageio_ffmpeg.count_frames_and_secs(str(downloaded.path))
            provider_fps = provider_frames / provider_seconds if provider_seconds else 0.0
            if not provider_fps or provider_fps < fps * (1 - PROVIDER_FPS_TOLERANCE):
                raise ProviderFailure(
                    f"provider returned {provider_fps:.3f} fps for a {fps:g} fps source: "
                    "conforming it would duplicate frames"
                )
            if provider_frames < segment.min_frames:
                raise ProviderFailure(
                    f"provider returned {provider_frames} frames, need {segment.min_frames} "
                    f"(head {segment.keep_from} + body {segment.keep_frames})"
                )
            return {
                "index": segment.index, "provider": provider, "attempt_count": attempt_number,
                "attempts": attempts + [{"provider": provider, "status": "succeeded"}],
                "path": downloaded.path,
                "body_start_frame": segment.plan.body_start, "body_frames": segment.keep_frames,
                "provider_frames": provider_frames, "provider_fps": round(provider_fps, 4),
                "expected_frames": segment.upload_frames, "keep_from": segment.keep_from,
                "shortfall_frames": max(0, segment.upload_frames - provider_frames),
            }
        except Exception as exc:
            # 记完整信息：只留类型名时，"3 次尝试都失败"这条日志无法定位到底是
            # 供应商报错、下载失败还是网络超时。
            attempts.append({
                "provider": provider, "status": "failed", "error": type(exc).__name__,
                "detail": str(exc)[:400],
            })
    raise ProviderFailure(
        f"segment {segment.index} failed after {len(attempts)} attempts: "
        + "; ".join(f"{item['provider']}={item['error']}({item.get('detail', '')})" for item in attempts)
    )


def _media_metadata(path: Path) -> dict[str, Any]:
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        return next(reader)
    finally:
        reader.close()


def merge_segments(
    results: list[dict[str, Any]],
    segments: list[Segment],
    source: Path,
    work_dir: Path,
    max_resolution: int,
) -> tuple[Path, int]:
    """把上游结果按帧号拼回源片。返回（成片, 成片视频帧数）。"""
    source_meta = _media_metadata(source)
    source_width, source_height = source_meta["size"]
    scale = max_resolution / max(source_width, source_height)
    width = max(2, round(source_width * scale / 2) * 2)
    height = max(2, round(source_height * scale / 2) * 2)
    fps = float(source_meta.get("fps") or 30)
    normalized_dir = work_dir / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    for segment, result in zip(segments, results, strict=True):
        provider_frames = int(result["provider_frames"])
        if provider_frames < segment.min_frames:
            raise RuntimeError(
                f"segment {segment.index}: provider has {provider_frames} frames, need {segment.min_frames}"
            )
        filters = [
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        ]
        if float(result["provider_fps"]) > fps * (1 + PROVIDER_FPS_TOLERANCE):
            # 上游给回来的帧比源片密：降帧率只会丢帧，不会造帧，所以这里可以安全地
            # 用它对齐；反方向（需求帧率更高）在 _process_segment 就已经判失败了。
            filters.insert(0, f"fps={fps}")
        # 按帧号取正片，并把时间戳钉回源片的帧网格：不丢帧、不补帧、不留拍子。
        # 末尾再加 -r（见 _rate）：每个样本都要有 1/fps 的时长，否则拼接时
        # 每段接缝都会挤掉一帧的显示时间。
        filters.append(
            f"trim=start_frame={segment.keep_from}:end_frame={segment.keep_from + segment.keep_frames},"
            f"setpts=N/({fps}*TB)"
        )
        target = normalized_dir / f"segment-{segment.index:04d}.mp4"
        _run_ffmpeg([
            "-i", str(result["path"]), "-an",
            "-vf", ",".join(filters),
            "-r", _rate(fps),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target),
        ])
        normalized.append(target)
    concat_file = work_dir / "concat.txt"
    concat_file.write_text("".join(f"file '{path.as_posix()}'\n" for path in normalized), encoding="utf-8")
    video_only = work_dir / "merged-video.mp4"
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(video_only)])
    # 装配校验：成片帧数必须等于各段正片帧数之和（= 源片帧数）。缺帧、重帧都在这里暴露。
    expected_frames = sum(segment.keep_frames for segment in segments)
    merged_frames, merged_seconds = imageio_ffmpeg.count_frames_and_secs(str(video_only))
    merged_frames = int(merged_frames)
    if merged_frames != expected_frames:
        raise RuntimeError(f"merged video has {merged_frames} frames, expected {expected_frames}")
    # 帧数对不代表时间轴对：归一化漏掉 -r 时每段接缝会挤掉一帧的显示时间，
    # 帧数一帧不少，成片却比源片短（实测 5 段 = 少 0.1667s）。这里一并钉住。
    if merged_seconds and abs(merged_seconds - merged_frames / fps) > 1.5 / fps:
        raise RuntimeError(
            f"merged video spans {merged_seconds:.4f}s for {merged_frames} frames at {fps:g}fps,"
            f" expected about {merged_frames / fps:.4f}s"
        )
    final = work_dir / "upscaled.mp4"
    # 不要把音频「截短」的 -shortest：源片的音轨通常比视频轨短一点点（实测 44100Hz
    # 的 aac 帧 1024 样本，60.0004s vs 视频 60.0333s），带上它就会按音频长度**砍掉
    # 成片的最后一帧**（实测 1801 → 1800 个样本）。上面刚校准好的帧数守恒就白做了。
    _run_ffmpeg([
        "-i", str(video_only), "-i", str(source), "-map", "0:v:0", "-map", "1:a?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(final),
    ])
    return final, merged_frames


@celery_app.task(bind=True, name="control_plane.video_upscale", time_limit=14400, soft_time_limit=14340)
def upscale_video(self, request_data: dict[str, Any]) -> dict[str, Any]:
    with job_slot("video_upscale", on_wait=slot_wait_reporter(self)):
        return _upscale_video(self, request_data)


def _upscale_video(self, request_data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    job_id = str(self.request.id)
    started = time.perf_counter()
    work_dir = settings.video_upscale_work_dir / job_id
    input_dir, segment_dir, output_dir = work_dir / "input", work_dir / "segments", work_dir / "outputs"
    input_dir.mkdir(parents=True, exist_ok=True); output_dir.mkdir(parents=True, exist_ok=True)
    try:
        self.update_state(state="PROGRESS", meta={"stage": "downloading", "progress": 2})
        source = download_public_media(
            request_data["source_uri"], input_dir, "source", VIDEO_MEDIA,
            settings.video_upscale_max_download_bytes, settings.video_upscale_provider_timeout_seconds,
        ).path
        self.update_state(state="PROGRESS", meta={"stage": "splitting", "progress": 6})
        source_fps = float(_media_metadata(source).get("fps") or 0)
        if source_fps <= 0:
            raise RuntimeError("source video reports no frame rate")
        segments = split_video(
            source, segment_dir, settings.video_upscale_segment_seconds, source_fps,
            settings.video_upscale_head_context_frames, settings.video_upscale_tail_margin_frames,
        )
        segment_urls = [
            _upload_file(segment.upload, job_id, "media.video_upscale.segment_input", f"segment-{segment.index:04d}.mp4")
            for segment in segments
        ]
        self.update_state(state="PROGRESS", meta={
            "stage": "upscaling_segments", "progress": 12, "segment_count": len(segments), "completed_segments": 0,
        })
        results: dict[int, dict[str, Any]] = {}
        concurrency = max(1, min(segment_limit("video_upscale"), len(segments)))
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    _process_segment, segment, url, request_data,
                    settings.video_upscale_auto_provider_order,
                    settings.video_upscale_segment_attempts, output_dir, source_fps,
                ): segment.index
                for segment, url in zip(segments, segment_urls, strict=True)
            }
            for future in as_completed(futures):
                result = future.result()
                results[result["index"]] = result
                completed = len(results)
                self.update_state(state="PROGRESS", meta={
                    "stage": "upscaling_segments", "progress": 12 + int(73 * completed / len(segments)),
                    "segment_count": len(segments), "completed_segments": completed,
                })
        ordered = [results[index] for index in range(1, len(segments) + 1)]
        self.update_state(state="PROGRESS", meta={"stage": "merging", "progress": 88})
        final, output_frames = merge_segments(
            ordered, segments, source, work_dir, int(request_data["max_resolution"]),
        )
        self.update_state(state="PROGRESS", meta={"stage": "uploading", "progress": 95})
        result_url = _upload_file(final, job_id, "media.video_upscale", "upscaled.mp4")
        source_frames = sum(segment.keep_frames for segment in segments)
        return {
            "job_id": job_id, "status": "succeeded", "requested_provider": request_data.get("provider", "auto"),
            "provider": "mixed" if len({item["provider"] for item in ordered}) > 1 else ordered[0]["provider"],
            "fallback_used": any(item["attempt_count"] > 1 for item in ordered),
            "segment_count": len(ordered), "segment_seconds_limit": settings.video_upscale_segment_seconds,
            "fps": source_fps,
            "source_frames": source_frames, "output_frames": output_frames,
            "lossless_frames": output_frames == source_frames,
            "context_frames": {
                "head": settings.video_upscale_head_context_frames,
                "tail": settings.video_upscale_tail_margin_frames,
            },
            "segments_absorbed_shortfall": sum(1 for item in ordered if item["shortfall_frames"] > 0),
            "max_shortfall_frames": max(item["shortfall_frames"] for item in ordered),
            "segments": [{key: value for key, value in item.items() if key != "path"} for item in ordered],
            "result_url": result_url, "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
