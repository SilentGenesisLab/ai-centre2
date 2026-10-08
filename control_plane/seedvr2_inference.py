from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

# 请求方能传的模型名 → 权重文件名。**白名单而不是路径拼接**：
# 这个接口是公网的，把 model 参数当文件名用等于让人往磁盘上任意位置读。
#
# `3b` 指的是 **fp8** 那份，不是 fp16 —— 2026-10-03 在 4090 上按同一套 15 张图
# × 2 种劣化实测，fp8 对 fp16 是**三项全胜**：质量一样（LPIPS 0.184 vs 0.185、
# PSNR 26.51 vs 26.39）、显存减半（~6G vs ~10G）、单张快一倍（2.2s vs 4.4s，
# 加载也快，4.7s vs 21.2s）。fp16 没有任何一项占优，所以不放进来。
MODELS: dict[str, str] = {
    "3b": "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
    "7b-sharp": "seedvr2_ema_7b_sharp_fp8_e4m3fn.safetensors",
}

# 上游 CLI 里 VAE 不是可选项（DEFAULT_VAE），所以这里也不开放选择。
VAE_FILE = "ema_vae_fp16.safetensors"


class SeedVR2Inference:
    """SeedVR2 的进程内包装。

    **模型只加载一次**：上游 CLI 的 `process_single_file` 收一个 `runner_cache` 字典，
    里面有 `ctx` 和 `runner` 就复用、没有才加载。所以这里把那个字典攥在实例上，
    跨张、跨作业一直用同一个 —— 不这么做的话每张图都要重载一次，
    7B 光加载就 ~5 秒，一批参考图全耗在这上面。

    代价是**这个对象只能有一个实例活在一个进程里**，而且 worker 必须是
    threads 池 + 并发 1：prefork 的每个子进程都会各自加载一份，显存直接翻倍。
    """

    def __init__(self, source_dir: Path, model_dir: Path, model_file: str) -> None:
        self.source_dir = Path(source_dir)
        self.model_dir = Path(model_dir)
        self.model_file = model_file
        self._lock = threading.Lock()
        self._runner_cache: dict[str, Any] | None = None
        self._cli: Any = None

    @property
    def model_name(self) -> str:
        return self.model_file

    def _import_cli(self):
        """把 vendored 的 SeedVR2 源码目录挂上 sys.path 再导入它的 CLI。

        不 pip 安装：那份代码是 ComfyUI 自定义节点的仓库，装成包会往环境里塞
        一堆 ComfyUI 的依赖。直接当源码用，版本由我们 scp 上去的那份决定。
        """
        if self._cli is not None:
            return self._cli
        if not (self.source_dir / "inference_cli.py").is_file():
            raise RuntimeError(f"SeedVR2 source not found at {self.source_dir}")
        path = str(self.source_dir)
        if path not in sys.path:
            sys.path.insert(0, path)
        import inference_cli  # type: ignore[import-not-found]

        self._cli = inference_cli
        return inference_cli

    def _arguments(self) -> Any:
        """拿一份 CLI 的默认参数，只覆盖我们必须定的那几个。

        走 `parse_arguments()` 而不是自己拼 Namespace：`process_single_file`
        读的字段有几十个，手拼的话上游一加字段这里就静默拿到默认值。
        `parse_arguments` 读的是 `sys.argv`，而且长度为 1 时会自己塞 `--help`
        然后退出，所以临时换成一个「有输入文件但不会被用到」的 argv。
        """
        cli = self._import_cli()
        original = sys.argv
        sys.argv = ["seedvr2", "unused.png", "--output_format", "png"]
        try:
            args = cli.parse_arguments()
        finally:
            sys.argv = original
        args.model_dir = str(self.model_dir)
        args.dit_model = self.model_file
        args.output_format = "png"
        # 这两个开关决定了 runner_cache 里的模型会不会被复用（见 upstream 859 行）。
        args.cache_dit = True
        args.cache_vae = True
        return args

    def upscale(self, source: Path, target: Path, target_short_side: int) -> tuple[tuple[int, int], tuple[int, int]]:
        """把 source 放大/修复到短边 target_short_side，写成 PNG 到 target。

        返回 `(源尺寸, 输出尺寸)`。尺寸由这里给而不是让任务体自己去读：
        **PIL 只有这个 worker 的环境里保证有**，控制面那个 venv 里没有
        （实测 `.venv-control` 就没有 Pillow）。任务体一旦自己 import PIL，
        就等于给「图片超分」这条链路加了一个它其实不需要的依赖。
        """
        cli = self._import_cli()
        # 形参标注是 Path，但调用方传字符串是很自然的事（CLI 那条路就全是字符串），
        # 所以这里自己收口，别让 `.parent` 在下游炸。
        source, target = Path(source), Path(target)
        missing = [self.model_dir / name for name in (self.model_file, VAE_FILE)
                   if not (self.model_dir / name).is_file()]
        if missing:
            raise RuntimeError("SeedVR2 weights missing: " + ", ".join(p.name for p in missing))
        args = self._arguments()
        args.resolution = int(target_short_side)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 模型不是线程安全的，而且并发跑两张会把显存打满；闸门已经把并发压到 1，
        # 这把锁是防「同进程内被别的路径直接调用」——便宜且不会掩盖真问题。
        with self._lock:
            if self._runner_cache is None:
                self._runner_cache = {}
            written = cli.process_single_file(
                str(source),
                args,
                ["0"],
                output_path=str(target),
                format_auto_detected=False,
                runner_cache=self._runner_cache,
        )
        if not written or not target.is_file():
            raise RuntimeError("SeedVR2 produced no output")
        from PIL import Image

        with Image.open(source) as source_image:
            source_size = source_image.size
        with Image.open(target) as result_image:
            output_size = result_image.size
        return source_size, output_size

    def close(self) -> None:
        """放掉显存。切换模型或关闭 worker 时用。

        `VideoDiffusionInfer` 上没有 release/close（查过 src/core/infer.py），
        所以只能断开引用让 GC 回收，再清一次 CUDA 缓存。够用：
        这个进程只有这一个模型，断引用之后没有别的地方还握着它。
        """
        self._lock.acquire()
        try:
            self._runner_cache = None
        finally:
            self._lock.release()
        try:
            import gc

            import torch

            gc.collect()
            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 — 释放失败不该盖住调用方的异常
            pass
