from __future__ import annotations

import importlib.metadata
import sysconfig
from pathlib import Path


EXPECTED_VERSION = "3.1.0"


def main() -> None:
    version = importlib.metadata.version("mmdet")
    if version != EXPECTED_VERSION:
        raise RuntimeError(
            f"refusing to patch mmdet {version}; expected {EXPECTED_VERSION}"
        )

    package = Path(sysconfig.get_paths()["purelib"]) / "mmdet" / "models"
    replacements = {
        package / "__init__.py": (
            "# MuseTalk compatibility: register only the DWPose backbone.\n"
            "from .backbones import CSPNeXt\n\n"
            "__all__ = ['CSPNeXt']\n"
        ),
        package / "backbones" / "__init__.py": (
            "# MuseTalk compatibility: avoid unrelated mmcv CUDA operators.\n"
            "from .cspnext import CSPNeXt\n\n"
            "__all__ = ['CSPNeXt']\n"
        ),
        package / "layers" / "__init__.py": (
            "# MuseTalk compatibility: DWPose only requires CSPLayer.\n"
            "from .csp_layer import CSPLayer\n\n"
            "__all__ = ['CSPLayer']\n"
        ),
    }
    for path, content in replacements.items():
        if not path.is_file():
            raise RuntimeError(f"mmdet file is missing: {path}")
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
