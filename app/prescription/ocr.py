"""
Pluggable OCR engines.

Every engine implements ``read(image) -> list[OcrLine]``. Select one with the
``PRESCRIPTION_OCR_ENGINE`` environment variable:

  easyocr                 Pretrained EasyOCR detector + recogniser (default).
  claude                  Claude vision reads the whole page (best on handwriting;
                          sends the image to Anthropic). See vlm.py.
  local-vlm               A local vision-language model (Qwen3-VL) on this machine.
  trocr                   EasyOCR text detection + a TrOCR recogniser. Point
                          ``PRESCRIPTION_TROCR_MODEL`` at a fine-tuned
                          checkpoint directory to use your own model.
  package.module:factory  Any callable returning an object with ``name`` and
                          ``read(image)``; for fully custom models.

``PRESCRIPTION_OCR_GPU`` = "1" / "0" forces GPU on or off (default: auto).
"""

from __future__ import annotations

import importlib
import os
import statistics
import threading
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

DEFAULT_TROCR_MODEL = "microsoft/trocr-base-handwritten"


@dataclass
class OcrBox:
    text: str
    confidence: float
    # Axis-aligned box in pixels: x0, y0, x1, y1
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class OcrLine:
    text: str
    confidence: float
    # Vision-language readers: other plausible readings of the same line, tried
    # against the dictionary too, and a note shown to the user (verbatim text, dosing).
    alternatives: list[str] = field(default_factory=list)
    note: str | None = None


class OcrEngine(Protocol):
    name: str

    def read(self, image: np.ndarray) -> list[OcrLine]: ...


class OcrUnavailableError(RuntimeError):
    """Raised when the configured engine's dependencies are missing."""


def group_into_lines(boxes: list[OcrBox]) -> list[OcrLine]:
    """Merge word/phrase boxes that share a baseline into reading-order lines."""
    boxes = [b for b in boxes if b.text.strip()]
    if not boxes:
        return []
    median_h = statistics.median(b.height for b in boxes) or 1.0
    rows: list[list[OcrBox]] = []
    for box in sorted(boxes, key=lambda b: b.cy):
        if rows:
            row_cy = statistics.mean(b.cy for b in rows[-1])
            if abs(box.cy - row_cy) <= 0.5 * median_h:
                rows[-1].append(box)
                continue
        rows.append([box])

    lines = []
    for row in rows:
        row.sort(key=lambda b: b.x0)
        text = " ".join(b.text.strip() for b in row)
        confidence = statistics.mean(b.confidence for b in row)
        lines.append(OcrLine(text=text, confidence=round(float(confidence), 3)))
    return lines


def _use_gpu() -> bool:
    flag = os.environ.get("PRESCRIPTION_OCR_GPU", "").strip()
    if flag in {"0", "1"}:
        return flag == "1"
    try:
        import torch

        return torch.cuda.is_available()
    except ImportError:
        return False


def _quad_to_box(quad, text: str, confidence: float) -> OcrBox:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return OcrBox(text, float(confidence), min(xs), min(ys), max(xs), max(ys))


class EasyOcrEngine:
    name = "easyocr"

    def __init__(self, languages: list[str] | None = None, gpu: bool | None = None):
        try:
            import easyocr
        except ImportError as exc:
            raise OcrUnavailableError(
                "EasyOCR is not installed. Run: pip install easyocr"
            ) from exc
        # verbose=False: the model-download progress bar crashes on cp1252 Windows consoles.
        self._reader = easyocr.Reader(
            languages or ["en"], gpu=_use_gpu() if gpu is None else gpu, verbose=False
        )
        self._lock = threading.Lock()

    def read(self, image: np.ndarray) -> list[OcrLine]:
        # EasyOCR's torch models are not safe to call from several threads at once.
        with self._lock:
            results = self._reader.readtext(image, paragraph=False)
        return group_into_lines([_quad_to_box(q, t, c) for q, t, c in results])


class TrOcrEngine:
    """
    EasyOCR finds text regions; a TrOCR model reads each region.

    This is the plug-in point for a fine-tuned handwriting model: train a
    VisionEncoderDecoder checkpoint on prescription word/line crops, save it
    with ``save_pretrained``, and set PRESCRIPTION_TROCR_MODEL to that folder.
    """

    name = "trocr"

    def __init__(self, model_path: str | None = None, gpu: bool | None = None):
        try:
            import easyocr
            import torch
            from transformers import TrOCRProcessor, VisionEncoderDecoderModel
        except ImportError as exc:
            raise OcrUnavailableError(
                "TrOCR needs extra packages. Run: pip install transformers easyocr"
            ) from exc
        model_path = model_path or os.environ.get("PRESCRIPTION_TROCR_MODEL", DEFAULT_TROCR_MODEL)
        use_gpu = _use_gpu() if gpu is None else gpu
        self._torch = torch
        self._device = "cuda" if use_gpu else "cpu"
        self._detector = easyocr.Reader(["en"], gpu=use_gpu, recognizer=False, verbose=False)
        self._processor = TrOCRProcessor.from_pretrained(model_path)
        self._model = VisionEncoderDecoderModel.from_pretrained(model_path).to(self._device)
        self._model.eval()
        self.name = f"trocr:{os.path.basename(os.path.normpath(model_path))}"
        self._lock = threading.Lock()

    def read(self, image: np.ndarray) -> list[OcrLine]:
        from PIL import Image

        with self._lock:
            horizontal, _free = self._detector.detect(image)
            regions = horizontal[0] if horizontal else []
            h, w = image.shape[:2]
            crops, coords = [], []
            for x0, x1, y0, y1 in regions:
                x0, y0 = max(0, int(x0)), max(0, int(y0))
                x1, y1 = min(w, int(x1)), min(h, int(y1))
                if x1 - x0 < 4 or y1 - y0 < 4:
                    continue
                crops.append(Image.fromarray(image[y0:y1, x0:x1]))
                coords.append((x0, y0, x1, y1))
            if not crops:
                return []

            boxes: list[OcrBox] = []
            batch_size = 16
            for start in range(0, len(crops), batch_size):
                batch = crops[start:start + batch_size]
                pixels = self._processor(images=batch, return_tensors="pt").pixel_values
                with self._torch.no_grad():
                    out = self._model.generate(
                        pixels.to(self._device),
                        max_new_tokens=32,
                        output_scores=True,
                        return_dict_in_generate=True,
                    )
                texts = self._processor.batch_decode(out.sequences, skip_special_tokens=True)
                scores = self._model.compute_transition_scores(
                    out.sequences, out.scores, normalize_logits=True
                )
                pad_id = self._model.generation_config.pad_token_id
                for i, text in enumerate(texts):
                    # Shorter lines in a batch are padded after EOS; those steps are not
                    # part of the prediction and must not drag the confidence down.
                    generated = out.sequences[i, -scores.shape[1]:]
                    token_scores = scores[i][(generated != pad_id) & (scores[i] > -1e9)]
                    confidence = float(token_scores.mean().exp()) if len(token_scores) else 0.0
                    x0, y0, x1, y1 = coords[start + i]
                    boxes.append(OcrBox(text, confidence, x0, y0, x1, y1))
        return group_into_lines(boxes)


_engine: OcrEngine | None = None
_engine_lock = threading.Lock()


def _build_engine(spec: str) -> OcrEngine:
    if spec == "easyocr":
        return EasyOcrEngine()
    if spec == "trocr":
        return TrOcrEngine()
    if spec == "claude":
        from app.prescription.vlm import ClaudeVisionEngine

        return ClaudeVisionEngine()
    if spec == "local-vlm":
        from app.prescription.vlm import LocalVisionEngine

        return LocalVisionEngine()
    if ":" in spec:
        module_name, attr = spec.split(":", 1)
        factory = getattr(importlib.import_module(module_name), attr)
        return factory()
    raise OcrUnavailableError(f"Unknown PRESCRIPTION_OCR_ENGINE: {spec!r}")


def get_engine() -> OcrEngine:
    """Load the configured engine once; model loading takes several seconds."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                spec = os.environ.get("PRESCRIPTION_OCR_ENGINE", "easyocr").strip() or "easyocr"
                _engine = _build_engine(spec)
    return _engine


def set_engine(engine: OcrEngine | None) -> None:
    """Override the engine (used by tests and custom start-up code)."""
    global _engine
    _engine = engine
