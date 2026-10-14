"""BiRefNet via ONNX Runtime: the production MaskModel adapter.

BiRefNet (bilateral reference network) is currently the strongest open model for
general dichotomous segmentation; it runs at a fixed 1024x1024 input.
"""

from __future__ import annotations

import logging
import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

log = logging.getLogger(__name__)

_SIZE = 1024
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)


class BiRefNet:
    """MaskModel adapter. Loads the model onto the GPU (falling back to CPU) and warms it up.

    Pre/post-processing runs on the caller's thread; only the forward pass is
    serialised, because one GPU gains nothing from concurrent runs of a 1024px net.
    """

    def __init__(
        self,
        model_path: Path,
        *,
        model_url: str | None = None,
        fp16: bool = True,
        use_tensorrt: bool = False,
    ):
        if not model_path.is_file():
            if not model_url:
                raise FileNotFoundError(model_path)
            _download(model_url, model_path)

        if use_tensorrt:
            _preload_tensorrt()
        ort.preload_dlls()
        if fp16 and not use_tensorrt and "CUDAExecutionProvider" in ort.get_available_providers():
            # TensorRT does its own fp16 from the fp32 graph; for CUDA we convert the graph.
            model_path = _fp16_variant(model_path)
        providers: list = []
        available = ort.get_available_providers()
        if use_tensorrt and "TensorrtExecutionProvider" in available:
            providers.append(
                (
                    "TensorrtExecutionProvider",
                    {
                        "trt_fp16_enable": True,
                        "trt_engine_cache_enable": True,
                        "trt_engine_cache_path": str(model_path.parent / "trt-cache"),
                    },
                )
            )
        if "CUDAExecutionProvider" in available:
            providers.append(("CUDAExecutionProvider", {"cudnn_conv_algo_search": "EXHAUSTIVE"}))
        providers.append("CPUExecutionProvider")

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.log_severity_level = 3
        try:
            self._session = ort.InferenceSession(str(model_path), opts, providers=providers)
        except Exception as exc:
            if "Failed to allocate memory" in str(exc):
                raise RuntimeError(
                    "Not enough free GPU memory to load BiRefNet (~3 GB needed). Is another copy of "
                    "the server, or another GPU program, already running? Check with `nvidia-smi`."
                ) from exc
            raise
        self._input = self._session.get_inputs()[0].name
        self._lock = threading.Lock()
        self.device = self._session.get_providers()[0]
        log.info("BiRefNet loaded on %s", self.device)
        self.predict(np.zeros((64, 64, 3), np.uint8))  # warm-up: cuDNN autotuning happens here

    def predict(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        interp = cv2.INTER_AREA if h * w > _SIZE * _SIZE else cv2.INTER_CUBIC
        x = cv2.resize(rgb, (_SIZE, _SIZE), interpolation=interp).astype(np.float32) / 255.0
        x = ((x - _MEAN) / _STD).transpose(2, 0, 1)[None]

        with self._lock:
            logits = self._session.run(None, {self._input: np.ascontiguousarray(x)})[0][0, 0]

        mask = 1.0 / (1.0 + np.exp(-logits))
        return cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32)


def _fp16_variant(model_path: Path) -> Path:
    """Half-precision copy of the model, created once next to the original.

    Measured on BiRefNet-general: ~1.35x faster on CUDA, mean |alpha diff| vs fp32 of 6e-5.
    """
    target = model_path.with_suffix(".fp16.onnx")
    if not target.is_file():
        import onnx
        from onnxconverter_common import float16

        log.warning("Converting %s to fp16 (one-time, ~40 s)", model_path.name)
        model = float16.convert_float_to_float16(onnx.load(str(model_path)), keep_io_types=True)
        tmp = target.with_suffix(".part")
        onnx.save(model, str(tmp))
        tmp.rename(target)
    return target


def _preload_tensorrt() -> None:
    """Make the pip-installed TensorRT 10 libraries visible to ONNX Runtime (optional extra)."""
    try:
        import ctypes
        import tensorrt_libs

        libdir = Path(tensorrt_libs.__file__).parent
        for name in ("libnvinfer.so.10", "libnvinfer_plugin.so.10", "libnvonnxparser.so.10"):
            ctypes.CDLL(str(libdir / name), mode=ctypes.RTLD_GLOBAL)
    except (ImportError, OSError) as exc:
        log.warning("TensorRT requested but unavailable (%s); using CUDA", exc)


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.warning("Model not found; downloading %s (~1 GB) to %s", url, dest)
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)
