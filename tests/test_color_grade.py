from pathlib import Path

from control_plane.api import ColorGradeUrlJobRequest
from control_plane.color_grade_tasks import _blend_opacity, _filter_path, _sanitize_cube


def test_color_grade_request_defaults_to_documented_strength() -> None:
    request = ColorGradeUrlJobRequest(
        video_url="https://media.example/video.mp4",
        cube_url="https://media.example/look.cube",
    )
    assert request.strength == 0.65


def test_blend_opacity_inverts_strength_because_ffmpeg_blends_on_first_input() -> None:
    # blend 的 all_opacity 作用在第一个输入（原片）上，normal 模式输出
    # top*opacity + bottom*(1-opacity)。滤镜图里 top 是原片、bottom 是 LUT，
    # 所以 strength=1（用满 LUT）必须换算成 opacity=0。
    assert _blend_opacity(1.0) == 0.0
    assert _blend_opacity(0.0) == 1.0
    assert abs(_blend_opacity(0.65) - 0.35) < 1e-9
    # 越界输入夹紧到 [0, 1]
    assert _blend_opacity(1.5) == 0.0
    assert _blend_opacity(-1.0) == 1.0


def test_filter_path_escapes_windows_separators_and_drive_colon() -> None:
    relative = _filter_path(Path("runtime") / "color-grade" / "job-id" / "lut_clean.cube")
    assert relative == "runtime/color-grade/job-id/lut_clean.cube"

    absolute = _filter_path(Path("C:/Users/dev/ai-centre/runtime/lut_clean.cube"))
    assert absolute == r"C\:/Users/dev/ai-centre/runtime/lut_clean.cube"

    posix = _filter_path(Path("/home/donxu/ai-centre/runtime/color-grade/job/lut_clean.cube"))
    assert posix == "/home/donxu/ai-centre/runtime/color-grade/job/lut_clean.cube"


def test_sanitize_cube_removes_comments_and_keeps_33_cube(tmp_path: Path) -> None:
    source = tmp_path / "input.cube"
    source.write_text(
        "TITLE \"test\"\nLUT_3D_SIZE 2\n" + "\n".join("0 0 0" for _ in range(8)),
        encoding="utf-8",
    )
    # Production accepts 17/33/65 point LUTs only.
    source.write_text(
        "TITLE \"test\"\nLUT_3D_SIZE 17\n"
        + "\n".join("0 0 0" for _ in range(17**3)),
        encoding="utf-8",
    )
    target = tmp_path / "clean.cube"
    assert _sanitize_cube(source, target) == 17
    assert target.read_text(encoding="ascii").startswith("LUT_3D_SIZE 17\n")
