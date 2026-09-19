from pathlib import Path

from control_plane.api import ColorGradeUrlJobRequest
from control_plane.color_grade_tasks import _filter_path, _sanitize_cube


def test_color_grade_request_defaults_to_documented_strength() -> None:
    request = ColorGradeUrlJobRequest(
        video_url="https://media.example/video.mp4",
        cube_url="https://media.example/look.cube",
    )
    assert request.strength == 0.65


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
