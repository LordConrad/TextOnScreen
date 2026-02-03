import sys
import os
import winreg
import tempfile
import traceback
import base64
import ctypes
import json
import io
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import re
import shutil
import importlib
import threading
import asyncio
from pathlib import Path
from datetime import datetime


def _prepare_image_for_ocr(img, scale: int, use_greedy: bool):
    """Prepare PIL image for OCR with sane bounds.

    The previous implementation always upscaled by `scale`, which can explode the pixel count
    for large selections (e.g. 4K * scale=2 -> ~33MP) and make OCR take minutes.

    This helper keeps user-requested upscale for small crops, but caps the final resolution
    so OCR stays typically in the order of seconds.
    """
    pil_image = importlib.import_module("PIL.Image")

    try:
        s = int(scale or 1)
    except Exception:
        s = 1
    s = max(1, min(s, 4))

    # Convert early so resize works consistently.
    img = img.convert("RGB")

    w, h = img.size
    if w <= 0 or h <= 0:
        return img

    # Target size after user upscale.
    tw = max(1, int(w * s))
    th = max(1, int(h * s))

    # Hard caps to keep OCR fast.
    # Greedy/fast mode gets a lower cap.
    max_pixels = 1_600_000 if use_greedy else 2_600_000
    max_long_edge = 1600 if use_greedy else 2200

    # Compute a limiter to respect caps.
    limiter = 1.0
    try:
        if tw * th > max_pixels:
            limiter = min(limiter, (max_pixels / float(tw * th)) ** 0.5)
    except Exception:
        pass
    try:
        long_edge = max(tw, th)
        if long_edge > max_long_edge:
            limiter = min(limiter, max_long_edge / float(long_edge))
    except Exception:
        pass

    fw = max(1, int(tw * limiter))
    fh = max(1, int(th * limiter))

    # Avoid needless work if size is unchanged.
    if fw == w and fh == h:
        return img

    # Use LANCZOS for both downscale and moderate upscale.
    return img.resize((fw, fh), resample=pil_image.LANCZOS)


def _light_ocr_preprocess(img):
    """Cheap preprocessing that can help UI text detection.

    Used only as a retry path when OCR returns no text.
    """
    try:
        pil_ops = importlib.import_module("PIL.ImageOps")
        pil_enhance = importlib.import_module("PIL.ImageEnhance")
        pil_filter = importlib.import_module("PIL.ImageFilter")

        x = img.convert("RGB")
        x = pil_ops.autocontrast(x)
        x = pil_enhance.Contrast(x).enhance(1.2)
        x = x.filter(pil_filter.UnsharpMask(radius=2, percent=150, threshold=3))
        return x
    except Exception:
        return img


def _run_async_sync(coro):
    """Run an async WinRT coroutine from a worker thread safely."""
    try:
        loop = asyncio.get_running_loop()
    except Exception:
        loop = None

    if loop and loop.is_running():
        new_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(new_loop)
            return new_loop.run_until_complete(coro)
        finally:
            try:
                new_loop.close()
            except Exception:
                pass
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass

    return asyncio.run(coro)


def _windows_ocr_available() -> bool:
    if os.name != "nt":
        return False
    try:
        importlib.import_module("winsdk.windows.media.ocr")
        importlib.import_module("winsdk.windows.graphics.imaging")
        importlib.import_module("winsdk.windows.storage.streams")
        return True
    except Exception:
        pass
    try:
        importlib.import_module("winrt.windows.media.ocr")
        importlib.import_module("winrt.windows.graphics.imaging")
        importlib.import_module("winrt.windows.storage.streams")
        return True
    except Exception:
        return False


def _windows_ocr_from_png_bytes(png_bytes: bytes) -> str:
    """Windows OCR via WinRT (fast, no ML model downloads).

    Requires either:
      - pip install winsdk
    or the split winrt packages:
      - pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Globalization
    """

    async def _do() -> str:
        try:
            ocr_mod = importlib.import_module("winsdk.windows.media.ocr")
            imaging_mod = importlib.import_module("winsdk.windows.graphics.imaging")
            streams_mod = importlib.import_module("winsdk.windows.storage.streams")
            glob_mod = importlib.import_module("winsdk.windows.globalization")
        except Exception:
            ocr_mod = importlib.import_module("winrt.windows.media.ocr")
            imaging_mod = importlib.import_module("winrt.windows.graphics.imaging")
            streams_mod = importlib.import_module("winrt.windows.storage.streams")
            glob_mod = importlib.import_module("winrt.windows.globalization")

        OcrEngine = getattr(ocr_mod, "OcrEngine")
        BitmapDecoder = getattr(imaging_mod, "BitmapDecoder")
        InMemoryRandomAccessStream = getattr(streams_mod, "InMemoryRandomAccessStream")
        DataWriter = getattr(streams_mod, "DataWriter")
        Language = getattr(glob_mod, "Language")

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png_bytes)
        await writer.store_async()
        await writer.flush_async()
        try:
            writer.detach_stream()
        except Exception:
            pass

        try:
            stream.seek(0)
        except Exception:
            pass

        decoder = await BitmapDecoder.create_async(stream)
        bmp = await decoder.get_software_bitmap_async()

        engine = None
        try:
            engine = OcrEngine.try_create_from_user_profile_languages()
        except Exception:
            engine = None
        if engine is None:
            engine = OcrEngine.try_create_from_language(Language("en"))

        result = await engine.recognize_async(bmp)
        return (getattr(result, "text", "") or "")

    if os.name != "nt":
        raise RuntimeError("Windows OCR is only available on Windows.")
    if not png_bytes:
        return ""
    return str(_run_async_sync(_do()) or "").strip()


def _windows_ocr_from_png_bytes_detail(png_bytes: bytes) -> dict:
    """Windows OCR via WinRT returning word boxes.

    Returns: {"text": str, "items": [{"text": str, "box": [[x,y],...], "score": float}, ...]}
    Box coordinates are in the pixel space of the provided PNG.
    """

    async def _do() -> dict:
        try:
            ocr_mod = importlib.import_module("winsdk.windows.media.ocr")
            imaging_mod = importlib.import_module("winsdk.windows.graphics.imaging")
            streams_mod = importlib.import_module("winsdk.windows.storage.streams")
            glob_mod = importlib.import_module("winsdk.windows.globalization")
        except Exception:
            ocr_mod = importlib.import_module("winrt.windows.media.ocr")
            imaging_mod = importlib.import_module("winrt.windows.graphics.imaging")
            streams_mod = importlib.import_module("winrt.windows.storage.streams")
            glob_mod = importlib.import_module("winrt.windows.globalization")

        OcrEngine = getattr(ocr_mod, "OcrEngine")
        BitmapDecoder = getattr(imaging_mod, "BitmapDecoder")
        InMemoryRandomAccessStream = getattr(streams_mod, "InMemoryRandomAccessStream")
        DataWriter = getattr(streams_mod, "DataWriter")
        Language = getattr(glob_mod, "Language")

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png_bytes)
        await writer.store_async()
        await writer.flush_async()
        try:
            writer.detach_stream()
        except Exception:
            pass

        try:
            stream.seek(0)
        except Exception:
            pass

        decoder = await BitmapDecoder.create_async(stream)
        bmp = await decoder.get_software_bitmap_async()

        engine = None
        try:
            engine = OcrEngine.try_create_from_user_profile_languages()
        except Exception:
            engine = None
        if engine is None:
            engine = OcrEngine.try_create_from_language(Language("en"))

        result = await engine.recognize_async(bmp)
        text = str(getattr(result, "text", "") or "")

        items: list[dict] = []

        def _rect_to_box(rect) -> list[list[float]] | None:
            if rect is None:
                return None
            try:
                x = float(getattr(rect, "x"))
                y = float(getattr(rect, "y"))
                w = float(getattr(rect, "width"))
                h = float(getattr(rect, "height"))
            except Exception:
                return None
            if w <= 0 or h <= 0:
                return None
            return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]

        try:
            lines = getattr(result, "lines", None) or []
        except Exception:
            lines = []

        for ln in lines:
            try:
                words = getattr(ln, "words", None) or []
            except Exception:
                words = []
            for w in words:
                try:
                    wt = str(getattr(w, "text", "") or "").strip()
                except Exception:
                    wt = ""
                if not wt:
                    continue
                rect = None
                try:
                    rect = getattr(w, "bounding_rect", None)
                except Exception:
                    rect = None
                if rect is None:
                    try:
                        rect = getattr(w, "boundingRect", None)
                    except Exception:
                        rect = None
                box = _rect_to_box(rect)
                if not box:
                    continue
                items.append({"text": wt, "box": box, "score": 1.0})

        return {"text": text, "items": items}

    if os.name != "nt":
        raise RuntimeError("Windows OCR is only available on Windows.")
    if not png_bytes:
        return {"text": "", "items": []}
    out = _run_async_sync(_do())
    if isinstance(out, dict):
        return out
    return {"text": str(out or ""), "items": []}


def _get_easyocr_model_dir() -> str:
    """Return a persistent per-user directory for EasyOCR model files."""
    # Windows: use LOCALAPPDATA (survives reboot and temp cleanups)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        model_dir = Path(base) / "TxtOnScrn" / "EasyOCR"
        model_dir.mkdir(parents=True, exist_ok=True)
        return str(model_dir)

    # Other OSes / fallback: use XDG cache if available, otherwise ~/.cache
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        model_dir = Path(xdg_cache) / "TxtOnScrn" / "EasyOCR"
    else:
        model_dir = Path.home() / ".cache" / "TxtOnScrn" / "EasyOCR"
    model_dir.mkdir(parents=True, exist_ok=True)
    return str(model_dir)


def _get_paddleocr_model_dir() -> str:
    """Return PaddleOCR model/cache directory.

    PaddleOCR by default stores models under the user's home in `.paddleocr`.
    We keep this helper so the app can expose "Clear OCR model cache".
    """
    return str(Path.home() / ".paddleocr")


def _paddle_extract_text(ocr_result) -> list[str]:
    """Normalize PaddleOCR output into a list of text lines."""
    if not ocr_result:
        return []

    # PaddleOCR output shape differs across versions:
    # - legacy: [ [ [box, (text, score)], ... ] ]
    # - newer: list[dict] with keys like rec_text/rec_score
    # We'll walk the structure and extract common text fields while preserving order.

    seen: set[str] = set()
    out: list[str] = []

    def _add(s: str) -> None:
        t = (s or "").strip()
        if not t:
            return
        if t in seen:
            return
        seen.add(t)
        out.append(t)

    def _add_from_seq(seq) -> None:
        """Add text from common Paddle/PaddleX shapes.

        Handles:
        - ["text", 0.98] / ("text", 0.98)
        - ["line1", "line2", ...]
        """
        if not isinstance(seq, (list, tuple)):
            return
        if not seq:
            return
        # (text, score)
        if len(seq) == 2 and isinstance(seq[0], str) and isinstance(seq[1], (int, float)):
            _add(seq[0])
            return
        # list[str]
        if all(isinstance(x, str) for x in seq):
            for x in seq:
                _add(x)
            return
        # mixed: add any string members
        for x in seq:
            if isinstance(x, str):
                _add(x)

    def _walk(obj) -> None:
        if obj is None:
            return
        # IMPORTANT: Do not collect arbitrary strings from the structure.
        # Only extract from known OCR text fields/patterns; otherwise we may
        # accidentally prepend unrelated values (e.g., config strings like
        # 'min' / 'general').
        if isinstance(obj, str):
            return
        if isinstance(obj, dict):
            for k in ("rec_text", "rec_texts", "text", "texts", "transcription", "label"):
                v = obj.get(k)
                if isinstance(v, str):
                    _add(v)
                elif isinstance(v, (list, tuple)):
                    _add_from_seq(v)
            for v in obj.values():
                # Recurse only into containers, not plain strings.
                if isinstance(v, (dict, list, tuple)):
                    _walk(v)
            return
        if isinstance(obj, (list, tuple)):
            # Legacy tuple/list pattern: [box, (text, score)]
            if len(obj) == 2:
                rec = obj[1]
                if isinstance(rec, (list, tuple)) and rec:
                    _add_from_seq(rec)
                if isinstance(rec, dict):
                    for k in ("rec_text", "text"):
                        v = rec.get(k)
                        if isinstance(v, str):
                            _add(v)
            for v in obj:
                if isinstance(v, (dict, list, tuple)):
                    _walk(v)
            return

    _walk(ocr_result)
    return out


def _paddle_extract_items(ocr_result) -> list[dict]:
    """Extract OCR items with bounding boxes from PaddleOCR output.

    Returns a list of dicts: {"text": str, "box": [[x,y], ...], "score": float|None}
    Box coordinates are in the OCR input image pixel space.
    """
    if not ocr_result:
        return []

    items: list[dict] = []

    def _norm_box(box):
        if not box:
            return None
        if isinstance(box, (list, tuple)) and len(box) == 4 and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in box):
            out = []
            for p in box:
                try:
                    out.append([float(p[0]), float(p[1])])
                except Exception:
                    return None
            return out
        return None

    def _add(text: str, box, score=None) -> None:
        t = (text or "").strip()
        b = _norm_box(box)
        if not t or not b:
            return
        try:
            sc = float(score) if score is not None else None
        except Exception:
            sc = None
        items.append({"text": t, "box": b, "score": sc})

    def _walk(obj) -> None:
        # Common legacy shape:
        # [ [ [box, (text, score)], ... ] ]
        if isinstance(obj, (list, tuple)):
            # [box, (text, score)]
            if len(obj) == 2 and isinstance(obj[0], (list, tuple)):
                box = obj[0]
                rec = obj[1]
                if isinstance(rec, (list, tuple)) and len(rec) >= 1:
                    text = rec[0]
                    score = rec[1] if len(rec) >= 2 else None
                    if isinstance(text, str):
                        _add(text, box, score)
                elif isinstance(rec, dict):
                    text = rec.get("rec_text") or rec.get("text")
                    score = rec.get("rec_score") or rec.get("score")
                    if isinstance(text, str):
                        _add(text, box, score)

            # Walk nested
            for v in obj:
                if isinstance(v, (dict, list, tuple)):
                    _walk(v)
            return

        if isinstance(obj, dict):
            # Newer PaddleX-ish shapes sometimes contain rec_text and polygon points
            text = obj.get("rec_text") or obj.get("text")
            box = obj.get("points") or obj.get("poly") or obj.get("box")
            score = obj.get("rec_score") or obj.get("score")
            if isinstance(text, str) and box is not None:
                _add(text, box, score)
            for v in obj.values():
                if isinstance(v, (dict, list, tuple)):
                    _walk(v)
            return

    _walk(ocr_result)
    return items


def _get_app_data_dir() -> str:
    """Return the persistent per-user app data directory used by TxtOnScrn."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return str(Path(base) / "TxtOnScrn")

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        return str(Path(xdg_cache) / "TxtOnScrn")

    return str(Path.home() / ".cache" / "TxtOnScrn")


def _ocr_worker_main() -> int:
    """CLI entrypoint used by the GUI process to run OCR in a separate process.

    Reads JSON from stdin and writes JSON to stdout.
    Input schema:
      {"image_path": str|null, "png_b64": str|null, "use_greedy": bool}
    Output schema:
      {"ok": true, "text": str} OR {"ok": false, "error": str}
    """
    try:
        # PaddleOCR 3.x uses PaddleX under the hood; disable model-host checks to avoid
        # long delays and noisy output.
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

        # Reduce noisy logs (Paddle/GLog).
        os.environ.setdefault("FLAGS_log_level", "3")
        os.environ.setdefault("GLOG_minloglevel", "3")

        proto_out = sys.stdout

        raw_in = sys.stdin.buffer.read()
        if not raw_in:
            raise RuntimeError("Missing stdin payload")
        payload = json.loads(raw_in.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid payload")

        image_path = payload.get("image_path")
        png_b64 = payload.get("png_b64")
        use_greedy = bool(payload.get("use_greedy", False))
        try:
            scale = int(payload.get("scale", 2) or 2)
        except Exception:
            scale = 2
        scale = max(1, min(scale, 4))

        # Avoid encoding crashes on some Windows setups.
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

        np = importlib.import_module("numpy")
        pil_image = importlib.import_module("PIL.Image")

        # Redirect any library prints away from stdout (stdout is reserved for JSON).
        try:
            sys.stdout = sys.stderr
        except Exception:
            pass

        paddleocr_mod = importlib.import_module("paddleocr")
        PaddleOCR = getattr(paddleocr_mod, "PaddleOCR")

        # PaddleOCR 3.x pipeline: disable heavy document-preprocess (unwarping etc.)
        # to keep OCR in seconds, not minutes.
        ocr = PaddleOCR(
            lang="en",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_det_limit_side_len=960,
        )

        if png_b64:
            png_bytes = base64.b64decode(png_b64.encode("ascii"), validate=False)
            img = pil_image.open(io.BytesIO(png_bytes))
        else:
            if not image_path:
                raise RuntimeError("Missing image input")
            img = pil_image.open(str(image_path))

        # Adaptive resize: keep small-font help, but cap huge selections for speed.
        img = _prepare_image_for_ocr(img, scale=scale, use_greedy=use_greedy)
        img_arr = np.array(img)

        # PaddleOCR expects BGR arrays (OpenCV-like)
        img_bgr = img_arr[:, :, ::-1]
        result = ocr.ocr(img_bgr)
        parts = _paddle_extract_text(result)
        out_text = "\n".join([p for p in parts if p]).strip()
        proto_out.write(json.dumps({"ok": True, "text": out_text}, ensure_ascii=False))
        return 0
    except ImportError:
        proto_out.write(
            json.dumps(
                {
                    "ok": False,
                    "error": "OCR Libraries Missing: missing 'paddleocr'/'paddlepaddle'. Please run: pip install paddlepaddle paddleocr",
                },
                ensure_ascii=False,
            )
        )
        try:
            proto_out.flush()
        except Exception:
            pass
        return 2
    except Exception as e:
        proto_out.write(
            json.dumps(
                {"ok": False, "error": f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"},
                ensure_ascii=False,
            )
        )
        try:
            proto_out.flush()
        except Exception:
            pass
        return 1


def _ocr_server_main() -> int:
    """Long-lived OCR process.

    Reads one JSON object per line from stdin and writes one JSON object per line to stdout.
    Caches OCR engine in-process so subsequent requests are fast.
    """
    try:
        # PaddleOCR 3.x uses PaddleX under the hood; disable model-host checks to avoid
        # long delays and noisy output.
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

        # Paddle 3.x on Windows can hit oneDNN/PIR execution-path issues (and/or become
        # extremely slow) in some environments. Disabling MKLDNN/oneDNN is a pragmatic
        # compatibility win for OCR.
        os.environ.setdefault("FLAGS_use_mkldnn", "0")

        # The observed crash is in PIR->runtime attribute conversion. Force legacy executor paths.
        os.environ.setdefault("FLAGS_enable_pir_in_executor", "0")
        os.environ.setdefault("FLAGS_enable_pir_api", "0")

        # Reduce noisy logs (Paddle/GLog).
        os.environ.setdefault("FLAGS_log_level", "3")
        os.environ.setdefault("GLOG_minloglevel", "3")

        proto_out = sys.stdout

        # Avoid noisy warnings interfering with protocols
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

        # Redirect any library prints away from stdout (stdout is reserved for JSON).
        try:
            sys.stdout = sys.stderr
        except Exception:
            pass

        np = importlib.import_module("numpy")
        pil_image = importlib.import_module("PIL.Image")

        # Handshake: let the parent process know the server started.
        # OCR engine initialization is lazy on first request.
        proto_out.write(json.dumps({"ok": True, "ready": True}, ensure_ascii=False) + "\n")
        proto_out.flush()

        ocr_fast = None
        ocr_full = None

        def _pil_to_bgr(pil_img):
            arr = np.array(pil_img)
            return arr[:, :, ::-1]

        for line in sys.stdin:
            line = (line or "").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise RuntimeError("Invalid payload")

                image_path = payload.get("image_path")
                png_b64 = payload.get("png_b64")
                use_greedy = bool(payload.get("use_greedy", False))
                return_boxes = bool(payload.get("return_boxes", False))
                try:
                    scale = int(payload.get("scale", 2) or 2)
                except Exception:
                    scale = 2
                scale = max(1, min(scale, 4))

                if png_b64:
                    png_bytes = base64.b64decode(png_b64.encode("ascii"), validate=False)
                    img = pil_image.open(io.BytesIO(png_bytes))
                else:
                    if not image_path:
                        raise RuntimeError("Missing image input")
                    img = pil_image.open(str(image_path))

                try:
                    orig_w, orig_h = int(img.size[0]), int(img.size[1])
                except Exception:
                    orig_w, orig_h = 0, 0

                # Adaptive resize: keep small-font help, but cap huge selections for speed.
                img = _prepare_image_for_ocr(img, scale=scale, use_greedy=use_greedy)
                try:
                    proc_w, proc_h = int(img.size[0]), int(img.size[1])
                except Exception:
                    proc_w, proc_h = orig_w, orig_h
                img_bgr = _pil_to_bgr(img)

                # Lazy init PaddleOCR to avoid slow startup / model downloads blocking READY.
                if ocr_fast is None or ocr_full is None:
                    paddleocr_mod = importlib.import_module("paddleocr")
                    PaddleOCR = getattr(paddleocr_mod, "PaddleOCR")
                    # Fast: smaller detector input.
                    ocr_fast = PaddleOCR(
                        lang="en",
                        use_doc_orientation_classify=False,
                        use_doc_unwarping=False,
                        use_textline_orientation=False,
                        text_det_limit_side_len=960,
                    )
                    # Full: slightly larger detector input (still capped by our image prep).
                    ocr_full = PaddleOCR(
                        lang="en",
                        use_doc_orientation_classify=False,
                        use_doc_unwarping=False,
                        use_textline_orientation=False,
                        text_det_limit_side_len=1280,
                    )

                ocr = ocr_fast if use_greedy else ocr_full
                result = ocr.ocr(img_bgr)
                parts = _paddle_extract_text(result)
                out_text = "\n".join([p for p in parts if p]).strip()

                items = _paddle_extract_items(result) if return_boxes else None

                # If fast mode returns nothing, retry once with the more accurate OCR.
                if use_greedy and not out_text and ocr_full is not None:
                    result2 = ocr_full.ocr(img_bgr)
                    parts2 = _paddle_extract_text(result2)
                    out_text = "\n".join([p for p in parts2 if p]).strip()
                    if return_boxes:
                        items = _paddle_extract_items(result2)

                # If still nothing, try a cheap preprocessing pass and retry with full mode.
                if not out_text and ocr_full is not None:
                    img2 = _light_ocr_preprocess(img)
                    img_bgr2 = _pil_to_bgr(img2)
                    result3 = ocr_full.ocr(img_bgr2)
                    parts3 = _paddle_extract_text(result3)
                    out_text = "\n".join([p for p in parts3 if p]).strip()

                    if return_boxes:
                        items = _paddle_extract_items(result3)

                resp = {"ok": True, "text": out_text}
                if return_boxes:
                    resp["items"] = items or []
                    resp["orig_size"] = {"w": orig_w, "h": orig_h}
                    resp["proc_size"] = {"w": proc_w, "h": proc_h}

                proto_out.write(json.dumps(resp, ensure_ascii=False) + "\n")
                proto_out.flush()
            except Exception as e:
                proto_out.write(
                    json.dumps(
                        {"ok": False, "error": f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                proto_out.flush()

        return 0
    except ImportError:
        proto_out.write(
            json.dumps(
                {
                    "ok": False,
                    "error": "OCR Libraries Missing: missing 'paddleocr'/'paddlepaddle'. Please run: pip install paddlepaddle paddleocr",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        try:
            proto_out.flush()
        except Exception:
            pass
        return 2
    except Exception:
        # Last-resort: don't print huge tracebacks that might break protocol consumers.
        return 1


# Run subprocess worker modes as early as possible (before Qt imports).
if "--ocr-worker" in sys.argv:
    raise SystemExit(_ocr_worker_main())
if "--ocr-server" in sys.argv:
    raise SystemExit(_ocr_server_main())

# Prevent Qt from applying High DPI scaling
# This ensures we get physical pixel coordinates (1920x1080) for all monitors
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
os.environ["QT_SCREEN_SCALE_FACTORS"] = "1"
os.environ["QT_SCALE_FACTOR"] = "1"

# OCR engine is optional and imported lazily (see EditorWindow.run_ocr).

import keyboard

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QRadioButton,
    QColorDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressDialog,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QSizePolicy,
    QSplitter,
    QSystemTrayIcon,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QSettings, QSharedMemory, QRect, QPoint, QObject, Signal, QEvent, QUrl, QThread, QBuffer, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QColor, QPen, QCursor, QPixmap, QPalette, QDesktopServices, QFont, QFontMetrics
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class _OcrWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self._ocr_fast = None
        self._ocr_full = None

    def run(self, payload) -> None:
        try:
            image_path = None
            png_bytes = None
            use_greedy = False

            if isinstance(payload, dict):
                image_path = payload.get("image_path")
                png_bytes = payload.get("png_bytes")
                use_greedy = bool(payload.get("use_greedy", False))
            else:
                image_path = str(payload)

            # Some Windows setups have non-UTF8 stdout; avoid encoding crashes.
            try:
                if hasattr(sys.stdout, "reconfigure"):
                    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                if hasattr(sys.stderr, "reconfigure"):
                    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

            np = importlib.import_module("numpy")
            pil_image = importlib.import_module("PIL.Image")
            paddleocr_mod = importlib.import_module("paddleocr")
            PaddleOCR = getattr(paddleocr_mod, "PaddleOCR")

            # Lazy-init and cache PaddleOCR instances within this worker thread (heavy)
            if self._ocr_fast is None or self._ocr_full is None:
                self._ocr_fast = PaddleOCR(
                    lang="en",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    text_det_limit_side_len=960,
                )
                self._ocr_full = PaddleOCR(
                    lang="en",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    text_det_limit_side_len=1280,
                )

            if png_bytes is not None:
                img = pil_image.open(io.BytesIO(png_bytes))
            else:
                if not image_path:
                    raise RuntimeError("Missing image input")
                img = pil_image.open(image_path)
            # Adaptive resize: keep small-font help, but cap huge selections for speed.
            scale = 2
            img = _prepare_image_for_ocr(img, scale=scale, use_greedy=use_greedy)
            img_arr = np.array(img)

            img_bgr = img_arr[:, :, ::-1]
            ocr = self._ocr_fast if use_greedy else self._ocr_full
            result = ocr.ocr(img_bgr)
            parts = _paddle_extract_text(result)
            out_text = "\n".join([p for p in parts if p]).strip()
            self.finished.emit(out_text)
        except ImportError:
            self.error.emit("OCR Libraries Missing: missing 'paddleocr'/'paddlepaddle'. Please run: pip install paddlepaddle paddleocr")
        except Exception as e:
            self.error.emit(f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}")


class _OcrSubprocessWorker(QObject):
    finished = Signal(str)
    finished_detail = Signal(object)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self._proc = None
        self._ready = False

    def _read_json_line(self, max_lines: int = 200):
        if not self._proc or not self._proc.stdout:
            raise RuntimeError("OCR server stdout is not available")
        non_json: list[str] = []
        for _ in range(max_lines):
            line = self._proc.stdout.readline()
            if not line:
                break
            s = (line or "").strip()
            if not s:
                continue
            try:
                return json.loads(s)
            except Exception:
                # PaddleOCR (or deps) may print non-JSON to stdout. Skip it.
                if len(non_json) < 8:
                    non_json.append(s[:300])
                continue
        msg = "OCR server returned no valid JSON"
        if non_json:
            msg += "\n\nServer stdout (non-JSON):\n" + "\n".join(non_json)
        raise RuntimeError(msg)

    def _read_json_line_with_timeout(self, timeout_s: float, max_lines: int = 200):
        result = {}
        error = {}

        def _target():
            try:
                result["resp"] = self._read_json_line(max_lines=max_lines)
            except Exception as e:
                error["exc"] = e

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        t.join(timeout_s)

        if t.is_alive():
            tail = self._proc_error_tail()
            self.shutdown()
            msg = f"OCR timeout after {timeout_s:.0f}s. PaddleOCR initialization/inference is stuck or extremely slow in this environment."
            if tail:
                msg += "\n\nServer stderr:\n" + tail
            raise TimeoutError(msg)

        if "exc" in error:
            raise error["exc"]
        return result.get("resp")

    def _proc_error_tail(self, max_chars: int = 4000) -> str:
        proc = self._proc
        if not proc:
            return ""
        if proc.poll() is None:
            return ""
        try:
            if proc.stderr:
                data = proc.stderr.read()
                data = (data or "")[-max_chars:]
                return data.strip()
        except Exception:
            return ""
        return ""

    def _ensure_proc(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        self._ready = False

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--ocr-server"]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--ocr-server"]

        env = os.environ.copy()
        env.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        env.setdefault("FLAGS_use_mkldnn", "0")
        env.setdefault("FLAGS_enable_pir_in_executor", "0")
        env.setdefault("FLAGS_enable_pir_api", "0")
        env.setdefault("PYTHONWARNINGS", "ignore")

        # stderr is captured so we can show crashes; stdout is reserved for JSON lines.
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        # Wait for READY handshake (blocks in worker thread, OK).
        try:
            resp = self._read_json_line(max_lines=50)
            if isinstance(resp, dict) and resp.get("ok") is True and resp.get("ready") is True:
                self._ready = True
                return
            if isinstance(resp, dict) and resp.get("ok") is False and resp.get("error"):
                raise RuntimeError(str(resp.get("error")))
            raise RuntimeError("OCR server did not send READY")
        except Exception as e:
            tail = self._proc_error_tail()
            raise RuntimeError(str(e) + ("\n\nServer stderr:\n" + tail if tail else ""))

    def shutdown(self) -> None:
        proc = self._proc
        self._proc = None
        self._ready = False
        if not proc:
            return
        try:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
        except Exception:
            pass

    def run(self, payload) -> None:
        try:
            if not isinstance(payload, dict):
                raise RuntimeError("Invalid OCR payload")

            want_detail = bool(payload.get("return_boxes", False))

            req = {
                "image_path": payload.get("image_path"),
                "png_b64": payload.get("png_b64"),
                "use_greedy": bool(payload.get("use_greedy", False)),
                "scale": payload.get("scale", 2),
            }

            if want_detail:
                req["return_boxes"] = True

            if not req.get("image_path") and not req.get("png_b64"):
                raise RuntimeError("Missing image input")

            self._ensure_proc()
            if not self._proc or self._proc.poll() is not None:
                raise RuntimeError("OCR server process is not running")
            if not self._ready:
                raise RuntimeError("OCR server is not ready")

            line = json.dumps(req, ensure_ascii=False)
            assert self._proc.stdin is not None
            assert self._proc.stdout is not None

            try:
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()
            except BrokenPipeError:
                # Server died; restart once and retry.
                tail = self._proc_error_tail()
                self.shutdown()
                self._ensure_proc()
                if not self._proc or self._proc.poll() is not None or not self._proc.stdin:
                    raise RuntimeError("OCR server crashed" + ("\n\nServer stderr:\n" + tail if tail else ""))
                self._proc.stdin.write(line + "\n")
                self._proc.stdin.flush()

            timeout_s = 60.0 if req.get("use_greedy") else 120.0
            resp = self._read_json_line_with_timeout(timeout_s=timeout_s, max_lines=50)
            if isinstance(resp, dict) and resp.get("ok") is True:
                if want_detail:
                    self.finished_detail.emit(resp)
                else:
                    self.finished.emit(str(resp.get("text") or ""))
                return

            if isinstance(resp, dict):
                msg = str(resp.get("error") or "OCR failed")
                tail = self._proc_error_tail()
                if tail:
                    msg += "\n\nServer stderr:\n" + tail
                self.error.emit(msg)
            else:
                self.error.emit("OCR failed: invalid response")
        except Exception as e:
            self.error.emit(f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}")


class _WindowsOcrWorker(QObject):
    finished = Signal(str)
    finished_detail = Signal(object)
    error = Signal(str)

    def run(self, payload) -> None:
        try:
            if os.name != "nt":
                raise RuntimeError("Windows OCR is only available on Windows")
            if not isinstance(payload, dict):
                raise RuntimeError("Invalid OCR payload")

            png_b64 = payload.get("png_b64")
            image_path = payload.get("image_path")
            use_greedy = bool(payload.get("use_greedy", True))
            try:
                scale = int(payload.get("scale", 1) or 1)
            except Exception:
                scale = 1
            scale = max(1, min(scale, 4))

            pil_image = importlib.import_module("PIL.Image")

            if png_b64:
                raw = base64.b64decode(str(png_b64).encode("ascii"), validate=False)
                img = pil_image.open(io.BytesIO(raw))
            else:
                if not image_path:
                    raise RuntimeError("Missing image input")
                img = pil_image.open(str(image_path))

            try:
                orig_w, orig_h = int(img.size[0]), int(img.size[1])
            except Exception:
                orig_w, orig_h = 0, 0

            img = _prepare_image_for_ocr(img, scale=scale, use_greedy=use_greedy)
            try:
                proc_w, proc_h = int(img.size[0]), int(img.size[1])
            except Exception:
                proc_w, proc_h = orig_w, orig_h
            buf = io.BytesIO()
            img.save(buf, format="PNG")

            want_detail = bool(payload.get("return_boxes", False))
            if want_detail:
                d = _windows_ocr_from_png_bytes_detail(buf.getvalue())
                text = str((d.get("text") if isinstance(d, dict) else "") or "").strip()
                items = (d.get("items") if isinstance(d, dict) else None) or []
                resp = {
                    "ok": True,
                    "text": text,
                    "items": items,
                    "orig_size": {"w": orig_w or proc_w, "h": orig_h or proc_h},
                    "proc_size": {"w": proc_w or orig_w, "h": proc_h or orig_h},
                }
                self.finished_detail.emit(resp)
            else:
                out_text = _windows_ocr_from_png_bytes(buf.getvalue())
                self.finished.emit((out_text or "").strip())
        except ImportError:
            self.error.emit(
                "Windows OCR needs WinRT Python bindings. Install one of these:\n"
                "- pip install winsdk\n"
                "  (recommended)\n"
                "or\n"
                "- pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams winrt-Windows.Globalization"
            )
        except Exception as e:
            self.error.emit(f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}")


class _EnhanceWorker(QObject):
    finished = Signal(bytes)
    error = Signal(str)

    def run(self, image_path: str) -> None:
        try:
            pil_image = importlib.import_module("PIL.Image")
            pil_ops = importlib.import_module("PIL.ImageOps")
            pil_enhance = importlib.import_module("PIL.ImageEnhance")
            pil_filter = importlib.import_module("PIL.ImageFilter")

            img = pil_image.open(image_path)
            img = img.convert("RGB")

            # Simple "enhance" pipeline (offline): upscale + autocontrast + mild sharpen
            scale = 2
            img = img.resize((img.size[0] * scale, img.size[1] * scale), resample=pil_image.LANCZOS)
            img = pil_ops.autocontrast(img)
            img = pil_enhance.Contrast(img).enhance(1.15)
            img = img.filter(pil_filter.UnsharpMask(radius=2, percent=160, threshold=3))

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self.finished.emit(buf.getvalue())
        except ImportError:
            self.error.emit("Enhance tool needs Pillow (PIL). Please run: pip install pillow")
        except Exception as e:
            self.error.emit(f"Enhance Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}")


ORG_NAME = "TxtOnScrn"
APP_NAME = "TrayConfig"
REG_APP_NAME = "TxtOnScrn"
SETTINGS_HOTKEY = "hotkey"
DEFAULT_HOTKEY = "Ctrl+Shift+Y"

SETTINGS_TRANSLATION_LANGUAGE = "translation_language"  # e.g. "en", "cs"
SETTINGS_TRANSLATION_SOURCE_LANGUAGE = "translation_source_language"  # "auto" | "en" | "cs" | ...
SETTINGS_TRANSLATION_HOTKEY = "translation_hotkey"
SETTINGS_TRANSLATION_ENGINE = "translation_engine"  # "argos" (offline) | "public" (online)
SETTINGS_TRANSLATION_AUTO_DOWNLOAD_MODELS = "translation_auto_download_models"  # bool
DEFAULT_TRANSLATION_LANGUAGE = "en"
DEFAULT_TRANSLATION_HOTKEY = "Ctrl+Shift+T"
DEFAULT_TRANSLATION_SOURCE_LANGUAGE = "auto"
DEFAULT_TRANSLATION_ENGINE = "argos"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
SETTINGS_THEME_MODE = "appearance_theme_mode"  # system | light | dark
SETTINGS_SNIP_BORDER_COLOR = "appearance_snip_border_color"  # hex, e.g. #0078D7
SETTINGS_SNIP_BORDER_WIDTH = "appearance_snip_border_width"  # int

SETTINGS_AI_OLLAMA_ENABLED = "ai_provider_ollama_enabled"
SETTINGS_AI_LMSTUDIO_ENABLED = "ai_provider_lmstudio_enabled"
SETTINGS_AI_LOCALAI_ENABLED = "ai_provider_localai_enabled"

SETTINGS_ASSISTANT_SELECTED = "assistant_selected"  # e.g. "local" or "public:groq"
SETTINGS_ASSISTANT_ANSWER_MD = "assistant_answer_md"  # bool: render/save answers as Markdown

SETTINGS_OCR_USE_GREEDY = "ocr_use_greedy"  # faster, slightly less accurate
SETTINGS_OCR_IN_MEMORY = "ocr_in_memory"  # avoid temp file I/O
SETTINGS_OCR_SCALE = "ocr_scale"  # int: 1..4 (speed vs accuracy)
SETTINGS_OCR_SCALE_AUTO = "ocr_scale_auto"  # bool
SETTINGS_OCR_ENGINE = "ocr_engine"  # "windows" | "paddle"
SETTINGS_OCR_AUTO_RUN = "ocr_auto_run"  # bool: run OCR automatically after capture
SETTINGS_OCR_ASSISTANT_AFTER_CAPTURE = "ocr_assistant_after_capture"  # bool: open Assistant instead of Editor

SETTINGS_AI_PUBLIC_GROQ_ENABLED = "ai_provider_public_groq_enabled"
SETTINGS_AI_PUBLIC_OPENAI_ENABLED = "ai_provider_public_openai_enabled"
SETTINGS_AI_PUBLIC_GEMINI_ENABLED = "ai_provider_public_gemini_enabled"
# (záměrně jednoduché – Public providery se přepínají jako rádio)

SETTINGS_AI_PUBLIC_GROQ_API_KEY = "ai_public_groq_api_key"
SETTINGS_AI_PUBLIC_OPENAI_API_KEY = "ai_public_openai_api_key"
SETTINGS_AI_PUBLIC_GEMINI_API_KEY = "ai_public_gemini_api_key"
SETTINGS_AI_GROQ_ENABLED = "ai_provider_groq_enabled"

DEFAULT_SNIP_BORDER_COLOR = "#aaaaff"
DEFAULT_SNIP_BORDER_WIDTH = 1


def _dpapi_encrypt_to_b64(text: str) -> str:
    """Windows DPAPI encrypt -> base64. Falls back to plain text if not available."""
    if not text:
        return ""
    if os.name != "nt":
        return text
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        raw = text.encode("utf-8")
        in_blob = DATA_BLOB(len(raw), (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw))
        out_blob = DATA_BLOB()

        if not crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            return text

        try:
            out_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return base64.b64encode(out_bytes).decode("ascii")
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return text


def _dpapi_decrypt_from_b64(value: str) -> str:
    """Windows DPAPI decrypt from base64. If value isn't encrypted, returns it as-is."""
    if not value:
        return ""
    if os.name != "nt":
        return value
    try:
        # If it's not base64, treat as plain
        try:
            enc = base64.b64decode(value.encode("ascii"), validate=True)
        except Exception:
            return value

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        in_blob = DATA_BLOB(len(enc), (ctypes.c_ubyte * len(enc)).from_buffer_copy(enc))
        out_blob = DATA_BLOB()

        if not crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob),
        ):
            return value

        try:
            out_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
            return out_bytes.decode("utf-8", errors="replace")
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return value


def get_windows_theme_mode() -> str:
    """Returns 'light' or 'dark' based on Windows settings (AppsUseLightTheme)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) == 1 else "dark"
    except Exception:
        return "light"


def apply_theme_mode(mode: str) -> None:
    """Applies 'light'/'dark' palette. Uses Windows mode for 'system'."""
    app = QApplication.instance()
    if app is None:
        return

    chosen = (mode or "system").lower()
    if chosen == "system":
        chosen = get_windows_theme_mode()

    app.setStyle("Fusion")

    palette = QPalette()
    if chosen == "dark":
        palette.setColor(QPalette.Window, QColor(32, 32, 32))
        palette.setColor(QPalette.WindowText, Qt.white)
        palette.setColor(QPalette.Base, QColor(24, 24, 24))
        palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
        palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
        palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
        palette.setColor(QPalette.Text, Qt.white)
        palette.setColor(QPalette.Button, QColor(45, 45, 45))
        palette.setColor(QPalette.ButtonText, Qt.white)
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.HighlightedText, Qt.black)
    else:
        # light
        palette.setColor(QPalette.Window, QColor(245, 245, 245))
        palette.setColor(QPalette.WindowText, QColor(0, 0, 0))
        palette.setColor(QPalette.Base, QColor(255, 255, 255))
        palette.setColor(QPalette.AlternateBase, QColor(235, 235, 235))
        palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 220))
        palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
        palette.setColor(QPalette.Text, QColor(0, 0, 0))
        palette.setColor(QPalette.Button, QColor(245, 245, 245))
        palette.setColor(QPalette.ButtonText, QColor(0, 0, 0))
        palette.setColor(QPalette.BrightText, Qt.red)
        palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.HighlightedText, Qt.white)

    app.setPalette(palette)

    # Ensure already-open windows/widgets repaint with the new palette
    for w in app.allWidgets():
        try:
            w.setPalette(app.palette())
            w.update()
        except Exception:
            pass


def resource_path(name: str) -> str:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base_path / name)


def get_startup_command() -> str:
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        exe_path = Path(sys.executable).resolve()
        return f'"{exe_path}"'
    else:
        # Running as Python script
        script_path = Path(__file__).resolve()
        python_exe = Path(sys.executable).resolve()
        return f'"{python_exe}" "{script_path}"'


def is_startup_enabled() -> bool:
    cmd = get_startup_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, REG_APP_NAME)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Startup check: Reg='{value}' vs App='{cmd}'")
            return value.lower() == cmd.lower()
    except FileNotFoundError:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Startup check: Registry key not found or value missing")
        return False
    except OSError as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Startup check error: {e}")
        return False


def set_startup_enabled(enabled: bool) -> None:
    command = get_startup_command()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Setting startup: {enabled} -> {command}")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, REG_APP_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, REG_APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as e:
        print(f"Registry error: {e}")


class ConfigTab(QWidget):
    def __init__(self, settings: QSettings, tray_app=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.tray_app = tray_app

        self.startup_checkbox = QCheckBox("Launch at startup")
        self.startup_checkbox.toggled.connect(self.on_startup_toggled)

        self.hotkey_label = QLabel()
        self.change_button = QPushButton("Change")
        self.change_button.clicked.connect(self.on_change_hotkey)

        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(QLabel("Hotkey:"))
        hotkey_row.addWidget(self.hotkey_label, 1)
        hotkey_row.addWidget(self.change_button)

        ocr_group = QGroupBox("OCR")
        ocr_layout = QVBoxLayout(ocr_group)

        ocr_engine_row = QHBoxLayout()
        ocr_engine_row.addWidget(QLabel("Engine:"))
        self.ocr_engine_combo = QComboBox()
        self.ocr_engine_combo.addItem("Windows OCR (fast)", "windows")
        self.ocr_engine_combo.addItem("PaddleOCR (models)", "paddle")
        self.ocr_engine_combo.setToolTip(
            "Windows OCR uses built-in Windows text recognition (very fast).\n"
            "PaddleOCR uses ML models (can be slower / needs downloads)."
        )
        self.ocr_engine_combo.currentIndexChanged.connect(self._on_ocr_engine_changed)
        ocr_engine_row.addWidget(self.ocr_engine_combo, 1)
        ocr_layout.addLayout(ocr_engine_row)

        self.ocr_greedy_cb = QCheckBox("Fast mode (greedy decoder)")
        self.ocr_in_memory_cb = QCheckBox("Use in-memory image (avoid temp file)")
        self.ocr_greedy_cb.toggled.connect(lambda checked: self.settings.setValue(SETTINGS_OCR_USE_GREEDY, bool(checked)))
        self.ocr_in_memory_cb.toggled.connect(lambda checked: self.settings.setValue(SETTINGS_OCR_IN_MEMORY, bool(checked)))
        ocr_layout.addWidget(self.ocr_greedy_cb)
        ocr_layout.addWidget(self.ocr_in_memory_cb)

        ocr_scale_row = QHBoxLayout()
        ocr_scale_row.addWidget(QLabel("Image upscale (1 = none):"))
        self.ocr_scale_spin = QSpinBox()
        self.ocr_scale_spin.setRange(1, 4)
        self.ocr_scale_spin.setToolTip(
            "1 = no upscale (fastest). Higher can help small fonts, but is slower.\n"
            "Tip: keep 'Automatic' ON for best speed.\n"
            "Large selections are capped internally so OCR doesn't take minutes."
        )
        self.ocr_scale_spin.valueChanged.connect(lambda v: self.settings.setValue(SETTINGS_OCR_SCALE, int(v)))
        ocr_scale_row.addWidget(self.ocr_scale_spin)
        ocr_scale_row.addWidget(QLabel("(1=none/fast, 2=default, 3-4=slower)"))
        ocr_scale_row.addStretch()
        ocr_layout.addLayout(ocr_scale_row)

        self.ocr_scale_auto_cb = QCheckBox("Automatic (pick scale based on selection size)")
        self.ocr_scale_auto_cb.toggled.connect(self._on_ocr_scale_auto_toggled)
        ocr_layout.addWidget(self.ocr_scale_auto_cb)

        self.ocr_auto_run_cb = QCheckBox("Auto-run OCR after capture")
        self.ocr_auto_run_cb.toggled.connect(lambda checked: self.settings.setValue(SETTINGS_OCR_AUTO_RUN, bool(checked)))
        ocr_layout.addWidget(self.ocr_auto_run_cb)

        self.ocr_assistant_after_capture_cb = QCheckBox("After capture: open AI assistant (skip editor)")
        self.ocr_assistant_after_capture_cb.toggled.connect(
            lambda checked: self.settings.setValue(SETTINGS_OCR_ASSISTANT_AFTER_CAPTURE, bool(checked))
        )
        ocr_layout.addWidget(self.ocr_assistant_after_capture_cb)

        self.ocr_clear_cache_btn = QPushButton("Clear PaddleOCR model cache")
        self.ocr_clear_cache_btn.setToolTip("Deletes cached PaddleOCR model files from disk. They will be re-downloaded on next OCR use.")
        self.ocr_clear_cache_btn.clicked.connect(self._on_clear_ocr_cache_clicked)
        ocr_layout.addWidget(self.ocr_clear_cache_btn)

        self.uninstall_btn = QPushButton("Uninstall / remove all app data")
        self.uninstall_btn.setToolTip("Disables startup, clears settings (registry), and deletes app data in LocalAppData.")
        self.uninstall_btn.clicked.connect(self._on_uninstall_clicked)

        layout = QVBoxLayout(self)
        layout.addWidget(self.startup_checkbox)
        layout.addLayout(hotkey_row)
        layout.addWidget(ocr_group)
        layout.addWidget(self.uninstall_btn)
        layout.addStretch()

        self.refresh()

    def refresh(self) -> None:
        is_enabled = is_startup_enabled()
        self.startup_checkbox.blockSignals(True)
        self.startup_checkbox.setChecked(is_enabled)
        self.startup_checkbox.blockSignals(False)

        hotkey = self.settings.value(SETTINGS_HOTKEY, DEFAULT_HOTKEY)
        self.hotkey_label.setText(hotkey)

        self.ocr_greedy_cb.blockSignals(True)
        self.ocr_greedy_cb.setChecked(bool(self.settings.value(SETTINGS_OCR_USE_GREEDY, True, type=bool)))
        self.ocr_greedy_cb.blockSignals(False)

        default_engine = "windows" if _windows_ocr_available() else "paddle"
        engine = str(self.settings.value(SETTINGS_OCR_ENGINE, default_engine) or default_engine).strip().lower()
        if engine not in ("windows", "paddle"):
            engine = default_engine
        self.ocr_engine_combo.blockSignals(True)
        idx = self.ocr_engine_combo.findData(engine)
        if idx >= 0:
            self.ocr_engine_combo.setCurrentIndex(idx)
        self.ocr_engine_combo.blockSignals(False)
        self._apply_ocr_engine_ui(engine)

        self.ocr_in_memory_cb.blockSignals(True)
        self.ocr_in_memory_cb.setChecked(bool(self.settings.value(SETTINGS_OCR_IN_MEMORY, False, type=bool)))
        self.ocr_in_memory_cb.blockSignals(False)

        try:
            scale = int(self.settings.value(SETTINGS_OCR_SCALE, 1) or 1)
        except Exception:
            scale = 1
        scale = max(1, min(scale, 4))
        self.ocr_scale_spin.blockSignals(True)
        self.ocr_scale_spin.setValue(scale)
        self.ocr_scale_spin.blockSignals(False)

        auto = bool(self.settings.value(SETTINGS_OCR_SCALE_AUTO, True, type=bool))
        self.ocr_scale_auto_cb.blockSignals(True)
        self.ocr_scale_auto_cb.setChecked(auto)
        self.ocr_scale_auto_cb.blockSignals(False)
        self.ocr_scale_spin.setEnabled(not auto)

        self.ocr_auto_run_cb.blockSignals(True)
        self.ocr_auto_run_cb.setChecked(bool(self.settings.value(SETTINGS_OCR_AUTO_RUN, False, type=bool)))
        self.ocr_auto_run_cb.blockSignals(False)

        self.ocr_assistant_after_capture_cb.blockSignals(True)
        self.ocr_assistant_after_capture_cb.setChecked(
            bool(self.settings.value(SETTINGS_OCR_ASSISTANT_AFTER_CAPTURE, False, type=bool))
        )
        self.ocr_assistant_after_capture_cb.blockSignals(False)

    def _on_ocr_engine_changed(self, *_args) -> None:
        engine = str(self.ocr_engine_combo.currentData() or "paddle").strip().lower()
        if engine not in ("windows", "paddle"):
            engine = "paddle"

        if engine == "windows" and not _windows_ocr_available():
            QMessageBox.information(
                self,
                "Windows OCR",
                "Windows OCR requires WinRT Python bindings.\n\n"
                "Recommended:\n"
                "- pip install winsdk\n\n"
                "After installing, restart the app.",
            )
        self.settings.setValue(SETTINGS_OCR_ENGINE, engine)
        self._apply_ocr_engine_ui(engine)

    def _apply_ocr_engine_ui(self, engine: str) -> None:
        engine = str(engine or "paddle").strip().lower()
        is_windows = engine == "windows"

        # Greedy decoder is Paddle-specific.
        try:
            self.ocr_greedy_cb.setEnabled(not is_windows)
        except Exception:
            pass

        # Paddle cache is irrelevant for Windows OCR.
        try:
            self.ocr_clear_cache_btn.setEnabled(not is_windows)
        except Exception:
            pass

    def _on_ocr_scale_auto_toggled(self, checked: bool) -> None:
        self.settings.setValue(SETTINGS_OCR_SCALE_AUTO, bool(checked))
        self.ocr_scale_spin.setEnabled(not bool(checked))

    def _on_clear_ocr_cache_clicked(self) -> None:
        model_dir = _get_paddleocr_model_dir()
        answer = QMessageBox.question(
            self,
            "Clear PaddleOCR cache",
            "This will delete cached PaddleOCR model files from disk.\n"
            "OCR will download them again the next time you run OCR.\n\n"
            f"Folder:\n{model_dir}\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        try:
            if os.path.isdir(model_dir):
                shutil.rmtree(model_dir, ignore_errors=False)
            Path(model_dir).mkdir(parents=True, exist_ok=True)
            QMessageBox.information(self, "Clear PaddleOCR cache", "PaddleOCR cache cleared.")
        except Exception as e:
            QMessageBox.warning(
                self,
                "Clear PaddleOCR cache",
                "Could not clear OCR cache. If OCR is currently running, close OCR windows and try again.\n\n"
                f"Error: {e}",
            )

    def _on_uninstall_clicked(self) -> None:
        app_dir = _get_app_data_dir()
        model_dir = _get_paddleocr_model_dir()

        answer = QMessageBox.question(
            self,
            "Uninstall / remove all data",
            "This will:\n"
            "- Disable startup (registry)\n"
            "- Clear all app settings (registry)\n"
            f"- Delete app data folder:\n{app_dir}\n"
            f"  (includes OCR model cache: {model_dir})\n\n"
            "The app will close afterwards. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        # Stop windows / OCR subprocesses first to release file locks.
        try:
            if self.tray_app is not None:
                try:
                    if getattr(self.tray_app, "ocr_assistant_flow", None) is not None:
                        self.tray_app.ocr_assistant_flow.shutdown()
                        self.tray_app.ocr_assistant_flow = None
                except Exception:
                    pass

                try:
                    if getattr(self.tray_app, "editor_window", None) is not None:
                        self.tray_app.editor_window.close()
                        self.tray_app.editor_window = None
                except Exception:
                    pass
        except Exception:
            pass

        errors = []
        try:
            set_startup_enabled(False)
        except Exception as e:
            errors.append(f"Startup: {e}")

        try:
            s = QSettings(ORG_NAME, APP_NAME)
            s.clear()
            s.sync()
        except Exception as e:
            errors.append(f"Settings: {e}")

        try:
            if os.path.isdir(app_dir):
                shutil.rmtree(app_dir)
        except Exception as e:
            errors.append(f"AppData: {e}")

        # Legacy: older versions stored EasyOCR models in %TEMP%\TxtOnScrn_EasyOCR
        try:
            legacy_dir = os.path.join(tempfile.gettempdir(), "TxtOnScrn_EasyOCR")
            if os.path.isdir(legacy_dir):
                shutil.rmtree(legacy_dir)
        except Exception as e:
            errors.append(f"LegacyTemp: {e}")

        if errors:
            QMessageBox.warning(
                self,
                "Uninstall / remove all data",
                "Cleanup finished with errors:\n\n" + "\n".join(errors) + "\n\nThe app will now close.",
            )
        else:
            QMessageBox.information(self, "Uninstall / remove all data", "Cleanup done. The app will now close.")

        # Exit the app after the dialog closes.
        if self.tray_app is not None:
            QTimer.singleShot(0, self.tray_app.quit)
        else:
            QTimer.singleShot(0, QApplication.quit)

    def on_startup_toggled(self, checked: bool) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Startup toggled: {checked}")
        set_startup_enabled(checked)

    def on_change_hotkey(self) -> None:
        dialog = HotkeyDialog(self.hotkey_label.text(), parent=self)
        if dialog.exec() == QDialog.Accepted:
            new_hotkey = dialog.hotkey_text.strip()
            if new_hotkey:
                self.hotkey_label.setText(new_hotkey)
                self.settings.setValue(SETTINGS_HOTKEY, new_hotkey)
                if self.tray_app:
                    self.tray_app.register_hotkeys()


class TranslationTab(QWidget):
    def __init__(self, settings: QSettings, tray_app=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.tray_app = tray_app

        lang_group = QGroupBox("Language")
        lang_layout = QVBoxLayout(lang_group)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("Auto (best effort)", "auto")
        self.source_combo.addItem("English", "en")
        self.source_combo.addItem("Czech (Čeština)", "cs")
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        src_row.addWidget(self.source_combo, 1)
        lang_layout.addLayout(src_row)

        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel("Target:"))
        self.language_combo = QComboBox()
        self.language_combo.addItem("English", "en")
        self.language_combo.addItem("Czech (Čeština)", "cs")
        self.language_combo.addItem("Slovak (Slovenčina)", "sk")
        self.language_combo.addItem("German (Deutsch)", "de")
        self.language_combo.addItem("French (Français)", "fr")
        self.language_combo.addItem("Spanish (Español)", "es")
        self.language_combo.addItem("Italian (Italiano)", "it")
        self.language_combo.addItem("Polish (Polski)", "pl")
        self.language_combo.addItem("Russian (Русский)", "ru")
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_row.addWidget(self.language_combo, 1)
        lang_layout.addLayout(lang_row)

        engine_group = QGroupBox("Engine")
        engine_layout = QVBoxLayout(engine_group)
        self.engine_offline_rb = QRadioButton("Offline (Argos Translate, open-source)")
        self.engine_public_rb = QRadioButton("Online (Public AI provider)")
        self.engine_offline_rb.toggled.connect(self._on_engine_changed)
        self.engine_public_rb.toggled.connect(self._on_engine_changed)
        engine_layout.addWidget(self.engine_offline_rb)
        engine_layout.addWidget(self.engine_public_rb)

        self.auto_models_cb = QCheckBox("Automatically download language models when missing (requires internet once)")
        self.auto_models_cb.toggled.connect(self._on_auto_models_toggled)
        engine_layout.addWidget(self.auto_models_cb)

        hotkey_group = QGroupBox("Shortcut")
        hotkey_layout = QVBoxLayout(hotkey_group)

        self.hotkey_label = QLabel()
        self.hotkey_change_btn = QPushButton("Change")
        self.hotkey_change_btn.clicked.connect(self._on_change_hotkey)

        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(QLabel("Hotkey:"))
        hotkey_row.addWidget(self.hotkey_label, 1)
        hotkey_row.addWidget(self.hotkey_change_btn)
        hotkey_layout.addLayout(hotkey_row)

        hint = QLabel(
            "Press the configured shortcut to open a translation overlay.\n"
            "Workflow: shortcut → capture screen → OCR → translation overlay.\n"
            "Tip: Offline translation uses Argos Translate models installed per language pair."
        )
        hint.setStyleSheet("color: rgba(0,0,0,0.65);")
        hotkey_layout.addWidget(hint)

        layout = QVBoxLayout(self)
        layout.addWidget(lang_group)
        layout.addWidget(engine_group)
        layout.addWidget(hotkey_group)
        layout.addStretch()

        self.refresh()

    def refresh(self) -> None:
        src = str(self.settings.value(SETTINGS_TRANSLATION_SOURCE_LANGUAGE, DEFAULT_TRANSLATION_SOURCE_LANGUAGE) or DEFAULT_TRANSLATION_SOURCE_LANGUAGE)
        src_idx = self.source_combo.findData(src)
        self.source_combo.blockSignals(True)
        if src_idx >= 0:
            self.source_combo.setCurrentIndex(src_idx)
        else:
            fallback = self.source_combo.findData(DEFAULT_TRANSLATION_SOURCE_LANGUAGE)
            if fallback >= 0:
                self.source_combo.setCurrentIndex(fallback)
        self.source_combo.blockSignals(False)

        lang = str(self.settings.value(SETTINGS_TRANSLATION_LANGUAGE, DEFAULT_TRANSLATION_LANGUAGE) or DEFAULT_TRANSLATION_LANGUAGE)
        idx = self.language_combo.findData(lang)
        self.language_combo.blockSignals(True)
        if idx >= 0:
            self.language_combo.setCurrentIndex(idx)
        else:
            # Default to English if unknown
            fallback = self.language_combo.findData(DEFAULT_TRANSLATION_LANGUAGE)
            if fallback >= 0:
                self.language_combo.setCurrentIndex(fallback)
        self.language_combo.blockSignals(False)

        engine = str(self.settings.value(SETTINGS_TRANSLATION_ENGINE, DEFAULT_TRANSLATION_ENGINE) or DEFAULT_TRANSLATION_ENGINE).strip().lower()
        if engine not in ("argos", "public"):
            engine = DEFAULT_TRANSLATION_ENGINE
        self.engine_offline_rb.blockSignals(True)
        self.engine_public_rb.blockSignals(True)
        self.engine_offline_rb.setChecked(engine == "argos")
        self.engine_public_rb.setChecked(engine == "public")
        self.engine_offline_rb.blockSignals(False)
        self.engine_public_rb.blockSignals(False)

        auto_models = bool(self.settings.value(SETTINGS_TRANSLATION_AUTO_DOWNLOAD_MODELS, True, type=bool))
        self.auto_models_cb.blockSignals(True)
        self.auto_models_cb.setChecked(auto_models)
        self.auto_models_cb.blockSignals(False)
        self.auto_models_cb.setEnabled(engine == "argos")

        hotkey = str(self.settings.value(SETTINGS_TRANSLATION_HOTKEY, DEFAULT_TRANSLATION_HOTKEY) or DEFAULT_TRANSLATION_HOTKEY)
        self.hotkey_label.setText(hotkey)

    def _on_source_changed(self, *_args) -> None:
        src = str(self.source_combo.currentData() or DEFAULT_TRANSLATION_SOURCE_LANGUAGE)
        self.settings.setValue(SETTINGS_TRANSLATION_SOURCE_LANGUAGE, src)

    def _on_language_changed(self, *_args) -> None:
        lang = str(self.language_combo.currentData() or DEFAULT_TRANSLATION_LANGUAGE)
        self.settings.setValue(SETTINGS_TRANSLATION_LANGUAGE, lang)

    def _on_engine_changed(self, *_args) -> None:
        engine = "argos" if self.engine_offline_rb.isChecked() else "public"
        self.settings.setValue(SETTINGS_TRANSLATION_ENGINE, engine)
        try:
            self.auto_models_cb.setEnabled(engine == "argos")
        except Exception:
            pass

    def _on_auto_models_toggled(self, checked: bool) -> None:
        self.settings.setValue(SETTINGS_TRANSLATION_AUTO_DOWNLOAD_MODELS, bool(checked))

    def _on_change_hotkey(self) -> None:
        dialog = HotkeyDialog(self.hotkey_label.text(), parent=self)
        if dialog.exec() == QDialog.Accepted:
            new_hotkey = dialog.hotkey_text.strip()
            if new_hotkey:
                self.hotkey_label.setText(new_hotkey)
                self.settings.setValue(SETTINGS_TRANSLATION_HOTKEY, new_hotkey)
                if self.tray_app:
                    self.tray_app.register_hotkeys()


class PublicAiClient:
    """Minimal Public AI client shared by non-UI flows (e.g. translation overlay)."""

    def __init__(self):
        self._groq_model = ""
        self._openai_model = ""
        self._gemini_model = ""

    @staticmethod
    def _get_selected_public_provider(settings: QSettings):
        if bool(settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, False, type=bool)):
            return "groq"
        if bool(settings.value(SETTINGS_AI_PUBLIC_OPENAI_ENABLED, False, type=bool)):
            return "openai"
        if bool(settings.value(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, False, type=bool)):
            return "gemini"
        return None

    @staticmethod
    def _get_public_api_key(settings: QSettings, provider: str) -> str:
        p = (provider or "").strip().lower()
        if p == "groq":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_GROQ_API_KEY, "") or "")
            return _dpapi_decrypt_from_b64(encrypted).strip()
        if p == "openai":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_OPENAI_API_KEY, "") or "")
            return _dpapi_decrypt_from_b64(encrypted).strip()
        if p == "gemini":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_GEMINI_API_KEY, "") or "")
            return _dpapi_decrypt_from_b64(encrypted).strip()
        return ""

    def _groq_select_model(self, api_key: str) -> str:
        preferences = [
            "llama-3.1-8b-instant",
            "llama-3.1-70b-versatile",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ]

        url = "https://api.groq.com/openai/v1/models"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return preferences[0]

        try:
            body = json.loads(raw)
        except Exception:
            return preferences[0]

        data = body.get("data") or []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        for pref in preferences:
            if pref in ids:
                return pref
        return ids[0] if ids else preferences[0]

    def _groq_generate(self, api_key: str, prompt: str) -> str:
        model = self._groq_model or self._groq_select_model(api_key)
        self._groq_model = model
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {raw_err}")

        body = json.loads(raw)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("No choices in response")
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if not content:
            raise RuntimeError("Empty response content")
        return content

    def _openai_generate(self, api_key: str, prompt: str) -> str:
        model = self._openai_model or "gpt-4o-mini"
        self._openai_model = model
        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {raw_err}")

        body = json.loads(raw)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("No choices in response")
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if not content:
            raise RuntimeError("Empty response content")
        return content

    def _gemini_generate(self, api_key: str, prompt: str) -> str:
        model = self._gemini_model or "gemini-1.5-flash"
        self._gemini_model = model

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            try:
                body_err = json.loads(raw_err)
            except Exception:
                body_err = None
            msg = None
            if isinstance(body_err, dict):
                err = body_err.get("error")
                if isinstance(err, dict):
                    msg = err.get("message")
            raise RuntimeError(f"HTTP {e.code}: {msg or raw_err}")

        body = json.loads(raw)
        candidates = body.get("candidates") or []
        if not candidates:
            raise RuntimeError("No candidates in response")

        content = (candidates[0].get("content") or {}) if isinstance(candidates[0], dict) else {}
        parts = content.get("parts") or []
        if not parts:
            raise RuntimeError("Empty response content")

        out = []
        for p in parts:
            if isinstance(p, dict) and p.get("text"):
                out.append(str(p.get("text")))
        text_out = "".join(out).strip()
        if not text_out:
            raise RuntimeError("Empty response text")
        return text_out

    def translate_text(self, text: str, target_lang_code: str) -> str:
        settings = QSettings(ORG_NAME, APP_NAME)
        provider = self._get_selected_public_provider(settings)
        if not provider:
            raise RuntimeError("No public AI provider selected. Configure it in Settings → AI → Public.")

        api_key = self._get_public_api_key(settings, provider)
        if not api_key:
            raise RuntimeError("Missing API key. Configure it in Settings → AI → Public.")

        lang_map = {
            "en": "English",
            "cs": "Czech",
            "sk": "Slovak",
            "de": "German",
            "fr": "French",
            "es": "Spanish",
            "it": "Italian",
            "pl": "Polish",
            "uk": "Ukrainian",
            "ru": "Russian",
        }
        target_name = lang_map.get((target_lang_code or "").strip().lower(), target_lang_code or "English")

        prompt = (
            "RULES:\n"
            "- Output ONLY the translated text.\n"
            "- Preserve line breaks as much as possible.\n"
            "- Do NOT add any explanations.\n"
            "- Do NOT wrap in quotes.\n\n"
            f"Translate the following text to {target_name}:\n\n"
            + (text or "")
        )

        if provider == "groq":
            return self._groq_generate(api_key=api_key, prompt=prompt).strip()
        if provider == "openai":
            return self._openai_generate(api_key=api_key, prompt=prompt).strip()
        if provider == "gemini":
            return self._gemini_generate(api_key=api_key, prompt=prompt).strip()
        raise RuntimeError("No supported public AI provider selected.")


def _guess_source_language_code(text: str) -> str:
    """Best-effort source language guess for offline translation.

    Argos Translate needs an explicit source language. We keep this intentionally
    simple: default to English unless Czech diacritics are detected.
    """
    s = (text or "")
    if not s:
        return "en"
    lowered = s.lower()
    cz_markers = "ěščřžýáíéůúďťňó"
    for ch in cz_markers:
        if ch in lowered:
            return "cs"
    return "en"


class LocalArgosTranslateClient:
    """Offline translation via Argos Translate (open-source).

    Models are installed per language pair. We keep all Argos data inside
    the app's LocalAppData folder by setting XDG/ARGOS env vars before import.
    """

    def __init__(self, base_dir: str | None = None):
        self._base_dir = Path(base_dir or _get_app_data_dir()) / "ArgosTranslate"

    def _configure_env(self) -> None:
        # Ensure Argos Translate stores everything under our app dir.
        data_dir = self._base_dir / "data"
        config_dir = self._base_dir / "config"
        cache_dir = self._base_dir / "cache"
        packages_dir = self._base_dir / "packages"

        data_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        packages_dir.mkdir(parents=True, exist_ok=True)

        os.environ["XDG_DATA_HOME"] = str(data_dir)
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["XDG_CACHE_HOME"] = str(cache_dir)
        os.environ["ARGOS_PACKAGES_DIR"] = str(packages_dir)

        # Keep CPU as default (most users).
        os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")

    def translate_text(self, text: str, source_lang_code: str, target_lang_code: str, auto_install: bool) -> str:
        self._configure_env()

        try:
            import argostranslate.package as argos_package
            import argostranslate.translate as argos_translate
        except Exception:
            raise RuntimeError(
                "Offline translation requires Argos Translate.\n\n"
                "Install: pip install argostranslate\n"
                "Then restart the app."
            )

        src = (source_lang_code or DEFAULT_TRANSLATION_SOURCE_LANGUAGE).strip().lower()
        dst = (target_lang_code or DEFAULT_TRANSLATION_LANGUAGE).strip().lower()

        if src in ("auto", ""):
            src = _guess_source_language_code(text)

        if not text or not text.strip():
            return ""
        if src == dst:
            return text.strip()

        def _try_translate() -> str:
            return str(argos_translate.translate(text, src, dst) or "").strip()

        try:
            out = _try_translate()
            if out:
                return out
        except Exception:
            pass

        if not auto_install:
            raise RuntimeError(
                f"No offline model installed for {src} → {dst}. "
                "Enable auto-download in Settings → Translation → Engine or install models via argospm."
            )

        # Try to install a direct model. If not available, try pivoting via English.
        try:
            argos_package.update_package_index()
        except Exception:
            # Continue; package index may already exist.
            pass

        installed = False
        try:
            installed = bool(argos_package.install_package_for_language_pair(src, dst))
        except Exception:
            installed = False

        if not installed and src != "en" and dst != "en":
            ok1 = False
            ok2 = False
            try:
                ok1 = bool(argos_package.install_package_for_language_pair(src, "en"))
            except Exception:
                ok1 = False
            try:
                ok2 = bool(argos_package.install_package_for_language_pair("en", dst))
            except Exception:
                ok2 = False
            installed = ok1 and ok2

        if not installed:
            raise RuntimeError(
                f"No Argos model available to install for {src} → {dst}. "
                "Try a different source/target language pair."
            )

        out = _try_translate()
        if not out:
            raise RuntimeError("Offline translation returned empty output.")
        return out


class _TranslateWorker(QObject):
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, text: str, target_lang: str, source_lang: str | None = None, parent=None):
        super().__init__(parent)
        self._text = text
        self._target_lang = target_lang
        self._source_lang = source_lang
        self._public_client = PublicAiClient()
        self._argos_client = LocalArgosTranslateClient()

    def run(self) -> None:
        try:
            settings = QSettings(ORG_NAME, APP_NAME)
            engine = str(settings.value(SETTINGS_TRANSLATION_ENGINE, DEFAULT_TRANSLATION_ENGINE) or DEFAULT_TRANSLATION_ENGINE).strip().lower()
            auto_models = bool(settings.value(SETTINGS_TRANSLATION_AUTO_DOWNLOAD_MODELS, True, type=bool))
            src = self._source_lang
            if src is None:
                src = str(settings.value(SETTINGS_TRANSLATION_SOURCE_LANGUAGE, DEFAULT_TRANSLATION_SOURCE_LANGUAGE) or DEFAULT_TRANSLATION_SOURCE_LANGUAGE)

            if engine == "public":
                out = self._public_client.translate_text(self._text, self._target_lang)
            else:
                out = self._argos_client.translate_text(
                    text=self._text,
                    source_lang_code=src,
                    target_lang_code=self._target_lang,
                    auto_install=auto_models,
                )
            self.finished.emit(out)
        except Exception as e:
            self.error.emit(str(e))


class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, tray_app=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.tray_app = tray_app

        self.setWindowTitle("Settings")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        self.resize(460, 320)

        self.tabs = QTabWidget()
        self.tab_name_to_index = {}

        config_tab = ConfigTab(settings=self.settings, tray_app=self.tray_app)
        self._add_tab(config_tab, "General")

        translation_tab = TranslationTab(settings=self.settings, tray_app=self.tray_app)
        self._add_tab(translation_tab, "Translation")

        appearance_tab = QWidget()
        appearance_layout = QVBoxLayout(appearance_tab)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Theme:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("System (Windows)", "system")
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        theme_row.addWidget(self.theme_combo, 1)

        appearance_layout.addLayout(theme_row)

        # Snipping selection border
        border_row = QHBoxLayout()
        border_row.addWidget(QLabel("Snip border:"))

        self.snip_border_preview = QLabel()
        self.snip_border_preview.setFixedSize(44, 18)
        border_row.addWidget(self.snip_border_preview)

        self.snip_border_color_btn = QPushButton("Color...")
        self.snip_border_color_btn.clicked.connect(self.on_pick_snip_border_color)
        border_row.addWidget(self.snip_border_color_btn)

        border_row.addWidget(QLabel("Width:"))
        self.snip_border_width = QSpinBox()
        self.snip_border_width.setRange(1, 10)
        self.snip_border_width.valueChanged.connect(self.on_snip_border_width_changed)
        border_row.addWidget(self.snip_border_width)

        border_row.addStretch()
        appearance_layout.addLayout(border_row)

        appearance_layout.addStretch()
        self._add_tab(appearance_tab, "Appearance")

        ai_tab = QWidget()
        ai_layout = QVBoxLayout(ai_tab)

        local_group = QGroupBox("Local")
        local_layout = QVBoxLayout(local_group)
        self.ai_ollama_cb = QCheckBox("Ollama (local server)")
        self.ai_lmstudio_cb = QCheckBox("LM Studio (local server)")
        self.ai_localai_cb = QCheckBox("LocalAI (OpenAI-compatible, local)")

        self.ai_ollama_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_ollama_cb, SETTINGS_AI_OLLAMA_ENABLED, checked))
        self.ai_lmstudio_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_lmstudio_cb, SETTINGS_AI_LMSTUDIO_ENABLED, checked))
        self.ai_localai_cb.toggled.connect(lambda checked: self.on_local_provider_toggled(self.ai_localai_cb, SETTINGS_AI_LOCALAI_ENABLED, checked))

        local_layout.addWidget(self.ai_ollama_cb)
        local_layout.addWidget(self.ai_lmstudio_cb)
        local_layout.addWidget(self.ai_localai_cb)

        public_group = QGroupBox("Public")
        public_layout = QVBoxLayout(public_group)
        self.ai_pub_groq_cb = QCheckBox("Groq")
        self.ai_pub_openai_cb = QCheckBox("OpenAI (ChatGPT)")
        self.ai_pub_gemini_cb = QCheckBox("Google Gemini")

        self.ai_pub_groq_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_groq_cb, SETTINGS_AI_PUBLIC_GROQ_ENABLED, checked))
        self.ai_pub_openai_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_openai_cb, SETTINGS_AI_PUBLIC_OPENAI_ENABLED, checked))
        self.ai_pub_gemini_cb.toggled.connect(lambda checked: self.on_public_provider_toggled(self.ai_pub_gemini_cb, SETTINGS_AI_PUBLIC_GEMINI_ENABLED, checked))

        public_layout.addWidget(self.ai_pub_groq_cb)
        public_layout.addWidget(self.ai_pub_openai_cb)
        public_layout.addWidget(self.ai_pub_gemini_cb)

        public_key_row = QHBoxLayout()
        self.public_api_key_label = QLabel("API key:")
        public_key_row.addWidget(self.public_api_key_label)
        self.public_api_key_edit = QLineEdit()
        self.public_api_key_edit.setEchoMode(QLineEdit.Password)
        self.public_api_key_edit.setPlaceholderText("Select provider and enter API key…")
        self.public_api_key_edit.textChanged.connect(self.on_public_key_changed)
        public_key_row.addWidget(self.public_api_key_edit, 1)

        self.public_key_show_btn = QPushButton("Show")
        self.public_key_show_btn.clicked.connect(self.on_toggle_public_key_visibility)
        public_key_row.addWidget(self.public_key_show_btn)

        self.public_key_clear_btn = QPushButton("Clear")
        self.public_key_clear_btn.clicked.connect(lambda: self.public_api_key_edit.setText(""))
        public_key_row.addWidget(self.public_key_clear_btn)
        public_layout.addLayout(public_key_row)

        self.public_status_label = QLabel("")
        public_layout.addWidget(self.public_status_label)

        public_buttons = QHBoxLayout()
        public_buttons.addStretch()
        self.ai_get_key_btn = QPushButton("Get API Key")
        self.ai_get_key_btn.clicked.connect(self.on_ai_get_key_clicked)
        public_buttons.addWidget(self.ai_get_key_btn)

        self.ai_test_key_btn = QPushButton("Test key")
        self.ai_test_key_btn.clicked.connect(self.on_ai_test_key_clicked)
        public_buttons.addWidget(self.ai_test_key_btn)

        public_layout.addLayout(public_buttons)

        ai_layout.addWidget(local_group)
        ai_layout.addWidget(public_group)

        assistant_group = QGroupBox("Assistant")
        assistant_layout = QVBoxLayout(assistant_group)
        self.assistant_answer_md_cb = QCheckBox("Answer in Markdown (.md) and open as output")
        self.assistant_answer_md_cb.setToolTip(
            "When enabled, Assistant output is saved to a .md file and opened.\n"
            "The output box also renders Markdown when supported by Qt."
        )
        self.assistant_answer_md_cb.toggled.connect(
            lambda checked: self.settings.setValue(SETTINGS_ASSISTANT_ANSWER_MD, bool(checked))
        )
        assistant_layout.addWidget(self.assistant_answer_md_cb)
        ai_layout.addWidget(assistant_group)

        ai_layout.addStretch()
        self._add_tab(ai_tab, "AI")

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.refresh_ui()

    def refresh_ui(self) -> None:
        # Sync theme selector from settings
        saved = (self.settings.value(SETTINGS_THEME_MODE, "system") or "system").lower()
        for i in range(self.theme_combo.count()):
            if self.theme_combo.itemData(i) == saved:
                self.theme_combo.blockSignals(True)
                self.theme_combo.setCurrentIndex(i)
                self.theme_combo.blockSignals(False)
                break

        # Sync snip border settings
        border_color = str(self.settings.value(SETTINGS_SNIP_BORDER_COLOR, DEFAULT_SNIP_BORDER_COLOR) or DEFAULT_SNIP_BORDER_COLOR)
        try:
            border_width = int(self.settings.value(SETTINGS_SNIP_BORDER_WIDTH, DEFAULT_SNIP_BORDER_WIDTH) or DEFAULT_SNIP_BORDER_WIDTH)
        except Exception:
            border_width = DEFAULT_SNIP_BORDER_WIDTH

        self._set_snip_border_preview(border_color)
        self.snip_border_width.blockSignals(True)
        self.snip_border_width.setValue(border_width)
        self.snip_border_width.blockSignals(False)

        # Sync AI provider checkboxes
        if hasattr(self, "ai_ollama_cb"):
            self.ai_ollama_cb.blockSignals(True)
            self.ai_ollama_cb.setChecked(bool(self.settings.value(SETTINGS_AI_OLLAMA_ENABLED, False, type=bool)))
            self.ai_ollama_cb.blockSignals(False)
        if hasattr(self, "ai_lmstudio_cb"):
            self.ai_lmstudio_cb.blockSignals(True)
            self.ai_lmstudio_cb.setChecked(bool(self.settings.value(SETTINGS_AI_LMSTUDIO_ENABLED, False, type=bool)))
            self.ai_lmstudio_cb.blockSignals(False)
        if hasattr(self, "ai_localai_cb"):
            self.ai_localai_cb.blockSignals(True)
            self.ai_localai_cb.setChecked(bool(self.settings.value(SETTINGS_AI_LOCALAI_ENABLED, False, type=bool)))
            self.ai_localai_cb.blockSignals(False)

        # Backward-compat: older keys map to Public providers
        if self.settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, None) is None:
            self.settings.setValue(SETTINGS_AI_PUBLIC_GROQ_ENABLED, bool(self.settings.value(SETTINGS_AI_GROQ_ENABLED, False, type=bool)))
        if hasattr(self, "ai_pub_groq_cb"):
            self.ai_pub_groq_cb.blockSignals(True)
            self.ai_pub_groq_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, False, type=bool)))
            self.ai_pub_groq_cb.blockSignals(False)
        if hasattr(self, "ai_pub_openai_cb"):
            self.ai_pub_openai_cb.blockSignals(True)
            self.ai_pub_openai_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_OPENAI_ENABLED, False, type=bool)))
            self.ai_pub_openai_cb.blockSignals(False)
        if hasattr(self, "ai_pub_gemini_cb"):
            self.ai_pub_gemini_cb.blockSignals(True)
            self.ai_pub_gemini_cb.setChecked(bool(self.settings.value(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, False, type=bool)))
            self.ai_pub_gemini_cb.blockSignals(False)

        self._refresh_public_key_ui()

        if hasattr(self, "assistant_answer_md_cb"):
            self.assistant_answer_md_cb.blockSignals(True)
            self.assistant_answer_md_cb.setChecked(bool(self.settings.value(SETTINGS_ASSISTANT_ANSWER_MD, False, type=bool)))
            self.assistant_answer_md_cb.blockSignals(False)

    def on_ai_get_key_clicked(self) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            QMessageBox.information(self, "API key", "Select a provider in the Public section.")
            return

        url = {
            "groq": "https://console.groq.com/keys",
            "openai": "https://platform.openai.com/api-keys",
            "gemini": "https://aistudio.google.com/app/apikey",
        }[provider]
        QDesktopServices.openUrl(QUrl(url))

    def on_ai_test_key_clicked(self) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            QMessageBox.information(self, "Test key", "Select a provider in the Public section.")
            return

        api_key = (self.public_api_key_edit.text() or "").strip()
        if not api_key:
            QMessageBox.information(self, "Test key", "Enter API key first.")
            return

        self.public_status_label.setText("Testing…")
        self.setCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, msg = self._test_public_key(provider, api_key)
            self.public_status_label.setText(("OK: " if ok else "Error: ") + msg)
        finally:
            self.setCursor(Qt.ArrowCursor)

    def on_toggle_public_key_visibility(self) -> None:
        if self.public_api_key_edit.echoMode() == QLineEdit.Password:
            self.public_api_key_edit.setEchoMode(QLineEdit.Normal)
            self.public_key_show_btn.setText("Hide")
        else:
            self.public_api_key_edit.setEchoMode(QLineEdit.Password)
            self.public_key_show_btn.setText("Show")

    def on_public_key_changed(self, value: str) -> None:
        provider = self._get_selected_public_provider()
        if not provider:
            return

        key_setting = {
            "groq": SETTINGS_AI_PUBLIC_GROQ_API_KEY,
            "openai": SETTINGS_AI_PUBLIC_OPENAI_API_KEY,
            "gemini": SETTINGS_AI_PUBLIC_GEMINI_API_KEY,
        }[provider]
        self.settings.setValue(key_setting, _dpapi_encrypt_to_b64(value))

    def on_local_provider_toggled(self, checkbox: QCheckBox, setting_key: str, checked: bool) -> None:
        if checked:
            for other_cb, other_key in (
                (self.ai_ollama_cb, SETTINGS_AI_OLLAMA_ENABLED),
                (self.ai_lmstudio_cb, SETTINGS_AI_LMSTUDIO_ENABLED),
                (self.ai_localai_cb, SETTINGS_AI_LOCALAI_ENABLED),
            ):
                if other_cb is checkbox:
                    continue
                other_cb.blockSignals(True)
                other_cb.setChecked(False)
                other_cb.blockSignals(False)
                self.settings.setValue(other_key, False)

        self.settings.setValue(setting_key, bool(checked))

    def on_public_provider_toggled(self, checkbox: QCheckBox, setting_key: str, checked: bool) -> None:
        if checked:
            for other_cb, other_key in (
                (self.ai_pub_groq_cb, SETTINGS_AI_PUBLIC_GROQ_ENABLED),
                (self.ai_pub_openai_cb, SETTINGS_AI_PUBLIC_OPENAI_ENABLED),
                (self.ai_pub_gemini_cb, SETTINGS_AI_PUBLIC_GEMINI_ENABLED),
            ):
                if other_cb is checkbox:
                    continue
                other_cb.blockSignals(True)
                other_cb.setChecked(False)
                other_cb.blockSignals(False)
                self.settings.setValue(other_key, False)

        self.settings.setValue(setting_key, bool(checked))
        self._refresh_public_key_ui()

    def _get_selected_public_provider(self):
        if self.ai_pub_groq_cb.isChecked():
            return "groq"
        if self.ai_pub_openai_cb.isChecked():
            return "openai"
        if self.ai_pub_gemini_cb.isChecked():
            return "gemini"
        return None

    def _refresh_public_key_ui(self) -> None:
        provider = self._get_selected_public_provider()
        enabled = provider is not None

        # Provider-aware label + placeholder
        if provider == "groq":
            self.public_api_key_label.setText("Groq API key:")
            setting_key = SETTINGS_AI_PUBLIC_GROQ_API_KEY
        elif provider == "openai":
            self.public_api_key_label.setText("OpenAI API key:")
            setting_key = SETTINGS_AI_PUBLIC_OPENAI_API_KEY
        elif provider == "gemini":
            self.public_api_key_label.setText("Gemini API key:")
            setting_key = SETTINGS_AI_PUBLIC_GEMINI_API_KEY
        else:
            self.public_api_key_label.setText("API key:")
            setting_key = None

        self.public_api_key_edit.setEnabled(enabled)
        self.public_key_show_btn.setEnabled(enabled)
        self.public_key_clear_btn.setEnabled(enabled)
        self.ai_get_key_btn.setEnabled(enabled)
        self.ai_test_key_btn.setEnabled(enabled)

        if not enabled:
            self.public_api_key_edit.blockSignals(True)
            self.public_api_key_edit.setText("")
            self.public_api_key_edit.blockSignals(False)
            self.public_status_label.setText("Select a provider in the Public section.")
            return

        stored = str(self.settings.value(setting_key, "") or "")
        decrypted = _dpapi_decrypt_from_b64(stored)
        self.public_api_key_edit.blockSignals(True)
        self.public_api_key_edit.setText(decrypted)
        self.public_api_key_edit.blockSignals(False)
        self.public_status_label.setText("")

    def _http_json(self, url: str, headers: dict):
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
            try:
                return resp.status, json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                return resp.status, {"raw": data.decode("utf-8", errors="replace")}

    def _test_public_key(self, provider: str, api_key: str):
        try:
            if provider == "groq":
                status, body = self._http_json(
                    "https://api.groq.com/openai/v1/models",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                )
                if status == 200:
                    return True, "Groq key is valid"
                return False, f"HTTP {status}"

            if provider == "openai":
                status, body = self._http_json(
                    "https://api.openai.com/v1/models",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                )
                if status == 200:
                    # Note: This only proves the key is accepted. Chat/completions may still fail
                    # if billing/quota is not enabled for the org/project.
                    return True, "OpenAI key je valid (pozn.: pro generování je potřeba mít aktivní billing/kvótu)"
                return False, f"HTTP {status}"

            if provider == "gemini":
                # Gemini (Google AI Studio) uses API key in query param.
                status, body = self._http_json(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(api_key)}",
                    headers={"Accept": "application/json"},
                )
                if status == 200:
                    return True, "Gemini API key je valid"
                return False, f"HTTP {status}"

            return False, "Unknown provider"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)

    def on_theme_changed(self) -> None:
        mode = self.theme_combo.currentData()
        self.settings.setValue(SETTINGS_THEME_MODE, mode)
        apply_theme_mode(mode)

    def _set_snip_border_preview(self, hex_color: str) -> None:
        color = QColor(hex_color)
        if not color.isValid():
            color = QColor(DEFAULT_SNIP_BORDER_COLOR)
        # simple swatch
        self.snip_border_preview.setStyleSheet(
            f"background-color: {color.name()}; border: 1px solid rgba(0,0,0,0.35);"
        )

    def on_pick_snip_border_color(self) -> None:
        current = str(self.settings.value(SETTINGS_SNIP_BORDER_COLOR, DEFAULT_SNIP_BORDER_COLOR) or DEFAULT_SNIP_BORDER_COLOR)
        initial = QColor(current)
        picked = QColorDialog.getColor(initial, self, "Pick snip border color")
        if not picked.isValid():
            return
        self.settings.setValue(SETTINGS_SNIP_BORDER_COLOR, picked.name())
        self._set_snip_border_preview(picked.name())

    def on_snip_border_width_changed(self, value: int) -> None:
        self.settings.setValue(SETTINGS_SNIP_BORDER_WIDTH, int(value))

    def _add_tab(self, widget: QWidget, name: str) -> None:
        idx = self.tabs.addTab(widget, name)
        self.tab_name_to_index[name.lower()] = idx

    def show_tab(self, name: str) -> None:
        idx = self.tab_name_to_index.get((name or "").lower())
        if idx is not None:
            self.tabs.setCurrentIndex(idx)

        # Refresh General tab on show (startup/hotkey may change outside)
        current = self.tabs.currentWidget()
        if isinstance(current, (ConfigTab, TranslationTab)):
            current.refresh()


class HotkeyDialog(QDialog):
    def __init__(self, current_hotkey: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Change Hotkey")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        self.hotkey_text = current_hotkey

        self.label = QLabel("Press new hotkey combination:")
        self.input = QLabel(current_hotkey)
        self.input.setFrameStyle(QLabel.Panel | QLabel.Sunken)

        self.buttons = QWidget()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout(self.buttons)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.input)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

    def keyPressEvent(self, event):  # noqa: N802
        keys = []
        if event.modifiers() & Qt.ControlModifier:
            keys.append("Ctrl")
        if event.modifiers() & Qt.ShiftModifier:
            keys.append("Shift")
        if event.modifiers() & Qt.AltModifier:
            keys.append("Alt")
        if event.modifiers() & Qt.MetaModifier:
            keys.append("Meta")
        key = event.key()
        if key not in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            key_sequence = QKeySequence(key)
            key_text = key_sequence.toString()
            if key_text:
                keys.append(key_text)
        if keys:
            self.hotkey_text = "+".join(keys)
            self.input.setText(self.hotkey_text)
        event.accept()


class HotkeySignal(QObject):
    hotkey_pressed = Signal()
    translate_pressed = Signal()


class MarkdownPreviewDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._file_path: str | None = None

        self.setWindowTitle("Markdown Output")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        self.resize(760, 560)

        layout = QVBoxLayout(self)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        layout.addWidget(self.browser, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.copy_btn = QPushButton("Copy Markdown")
        self.copy_btn.clicked.connect(self._copy_clicked)
        btn_row.addWidget(self.copy_btn)

        self.open_external_btn = QPushButton("Open .md externally")
        self.open_external_btn.clicked.connect(self._open_external_clicked)
        btn_row.addWidget(self.open_external_btn)

        self.save_as_btn = QPushButton("Save As…")
        self.save_as_btn.clicked.connect(self._save_as_clicked)
        btn_row.addWidget(self.save_as_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    def set_markdown(self, md_text: str, file_path: str | None = None) -> None:
        self._file_path = file_path
        s = str(md_text or "")
        try:
            if hasattr(self.browser, "setMarkdown"):
                self.browser.setMarkdown(s)
            else:
                # Fallback: show as plain text
                self.browser.setPlainText(s)
        except Exception:
            self.browser.setPlainText(s)

    def _copy_clicked(self) -> None:
        try:
            QApplication.clipboard().setText(self.browser.toPlainText())
        except Exception:
            pass

    def _open_external_clicked(self) -> None:
        if not self._file_path:
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._file_path))
        except Exception:
            pass

    def _save_as_clicked(self) -> None:
        default_name = "assistant_output.md"
        if self._file_path:
            try:
                default_name = os.path.basename(self._file_path)
            except Exception:
                default_name = "assistant_output.md"

        path, _flt = QFileDialog.getSaveFileName(self, "Save Markdown", default_name, "Markdown (*.md);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.browser.toPlainText())
        except Exception as e:
            QMessageBox.warning(self, "Save", f"Could not save: {e}")


class AssistantDialog(QDialog):
    def __init__(self, mode: str, get_editor_text, set_editor_text=None, open_settings=None, parent=None):
        super().__init__(parent)
        self.mode = (mode or "local").lower()
        self.get_editor_text = get_editor_text
        self.set_editor_text = set_editor_text
        self.open_settings = open_settings

        self.setWindowTitle("Assistant")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        self.resize(640, 420)

        layout = QVBoxLayout(self)

        # Menu bar (like Editor)
        self.menu_bar = QMenuBar()
        layout.addWidget(self.menu_bar)

        self.menu_input = self.menu_bar.addMenu("Input")
        self.menu_output = self.menu_bar.addMenu("Output")
        self.menu_assistant = self.menu_bar.addMenu("Assistant")

        copy_input_label = "Copy editor text" if callable(self.set_editor_text) else "Copy OCR text"
        self.act_copy_input = QAction(copy_input_label, self)
        self.act_copy_input.setShortcut(QKeySequence("Ctrl+Shift+I"))
        self.act_copy_input.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_copy_input.triggered.connect(self._copy_input_clicked)
        self.menu_input.addAction(self.act_copy_input)

        self.act_copy_output = QAction("Copy output", self)
        # Don't steal Ctrl+C from text fields; use Ctrl+Shift+C.
        self.act_copy_output.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self.act_copy_output.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_copy_output.triggered.connect(lambda: QApplication.clipboard().setText(self._current_output_plain_text()))
        self.menu_output.addAction(self.act_copy_output)

        self.menu_output.addSeparator()
        self.act_open_md = QAction("Open .md", self)
        self.act_open_md.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.act_open_md.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_open_md.triggered.connect(self._open_last_md_external)
        self.menu_output.addAction(self.act_open_md)

        self.act_save_md_as = QAction("Save output as…", self)
        self.act_save_md_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.act_save_md_as.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_save_md_as.triggered.connect(self._save_last_md_as)
        self.menu_output.addAction(self.act_save_md_as)

        self.act_settings = QAction("Settings…", self)
        self.act_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.act_settings.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_settings.triggered.connect(self._open_settings_clicked)
        self.menu_assistant.addAction(self.act_settings)

        self.act_help = QAction("Help", self)
        self.act_help.setShortcut(QKeySequence("F1"))
        self.act_help.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_help.triggered.connect(self._help_clicked)
        self.menu_assistant.addAction(self.act_help)

        self.menu_assistant.addSeparator()
        self.act_run = QAction("Run", self)
        self.act_run.setShortcut(QKeySequence("Ctrl+Return"))
        self.act_run.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_run.triggered.connect(self.on_run)
        self.menu_assistant.addAction(self.act_run)

        self.menu_assistant.addSeparator()
        self.act_close = QAction("Close", self)
        self.act_close.setShortcut(QKeySequence("Ctrl+W"))
        self.act_close.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self.act_close.triggered.connect(self.close)
        self.menu_assistant.addAction(self.act_close)

        # Assistant selection
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Assistant:"))
        self.assistant_combo = QComboBox()
        self.assistant_combo.addItem("Local (offline)", "local")
        self.assistant_combo.addItem("Public (Groq)", "public:groq")
        self.assistant_combo.addItem("Public (OpenAI)", "public:openai")
        self.assistant_combo.addItem("Public (Gemini)", "public:gemini")
        self.assistant_combo.currentIndexChanged.connect(self._on_assistant_changed)
        top_row.addWidget(self.assistant_combo, 1)
        layout.addLayout(top_row)

        layout.addWidget(QLabel("Enter task/question (can be unrelated to text)."))

        self.task_edit = QTextEdit()
        self.task_edit.setPlaceholderText("E.g.: Summarize into 5 points. / Fix typos. / Explain what this means... ")
        self.task_edit.installEventFilter(self)
        layout.addWidget(self.task_edit, 1)

        out_header = QHBoxLayout()
        out_header.addWidget(QLabel("Output:"))
        out_header.addStretch()
        self.output_status_label = QLabel("")
        out_header.addWidget(self.output_status_label)
        layout.addLayout(out_header)

        self.output_busy = QProgressBar()
        self.output_busy.setRange(0, 0)  # indeterminate
        self.output_busy.setTextVisible(False)
        self.output_busy.setVisible(False)
        layout.addWidget(self.output_busy)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        layout.addWidget(self.output_text, 2)

        self.output_md = QTextBrowser()
        self.output_md.setOpenExternalLinks(True)
        self.output_md.setVisible(False)
        layout.addWidget(self.output_md, 2)

        self._last_md_path: str | None = None
        self._sync_output_mode()

        self._set_initial_assistant_selection()
        self._on_assistant_changed()

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.apply_btn = QPushButton("Apply to editor")
        self.apply_btn.setEnabled(callable(self.set_editor_text))
        self.apply_btn.clicked.connect(self._apply_to_editor_clicked)
        buttons.addWidget(self.apply_btn)
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self.on_run)
        buttons.addWidget(self.run_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def set_run_enabled(self, enabled: bool) -> None:
        try:
            self.run_btn.setEnabled(bool(enabled))
        except Exception:
            pass

    def eventFilter(self, source, event):
        # Submit on Enter, allow newline on Shift+Enter
        if source == self.task_edit and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    return False
                self.on_run()
                return True
        return super().eventFilter(source, event)

    def _apply_to_editor_clicked(self) -> None:
        if not callable(self.set_editor_text):
            QMessageBox.information(self, "Apply", "Editor is not available.")
            return

        text = (self._current_output_plain_text() or "").strip()
        if not text:
            QMessageBox.information(self, "Apply", "No output to apply.")
            return

        try:
            self.set_editor_text(text)
        except Exception as e:
            QMessageBox.warning(self, "Apply", f"Could not apply to editor: {e}")

    def _copy_input_clicked(self) -> None:
        text = ""
        try:
            text = (self.get_editor_text() or "").strip()
        except Exception:
            text = ""

        if not text:
            QMessageBox.information(self, "Copy", "No input text to copy.")
            return

        try:
            QApplication.clipboard().setText(text)
        except Exception as e:
            QMessageBox.warning(self, "Copy", f"Could not copy to clipboard: {e}")

    def on_run(self):
        self._sync_output_mode()
        task = (self.task_edit.toPlainText() or "").strip()
        editor_text = ""
        try:
            editor_text = (self.get_editor_text() or "")
        except Exception:
            editor_text = ""

        if not task:
            self._set_output("Please enter a task/question.")
            return

        mode, provider = self._get_selected_assistant()
        if mode == "public":
            self._run_public(task=task, editor_text=editor_text, provider_override=provider)
            return

        # Local Assistant: simple offline text operations (no external APIs)
        text = (editor_text or "").strip()
        if not text:
            self._set_output(
                "Editor text is empty.\n\n"
                "Tip: First paste text into the editor, then try e.g.: 'Summarize into 5 points'."
            )
            return

        try:
            self._set_busy(True, "Working…")
            result = self._run_local(task=task, text=text)
        except Exception as e:
            result = f"Local assistant error: {e}"
        finally:
            self._set_busy(False)
        self._set_output(result)

    def _set_initial_assistant_selection(self) -> None:
        settings = QSettings(ORG_NAME, APP_NAME)

        # 1) Prefer a persisted selection (survives app restarts)
        saved = str(settings.value(SETTINGS_ASSISTANT_SELECTED, "") or "").strip()
        if saved:
            for i in range(self.assistant_combo.count()):
                if str(self.assistant_combo.itemData(i) or "") == saved:
                    self.assistant_combo.setCurrentIndex(i)
                    return

        # 2) Fallback: If the dialog was opened in public mode, default to the selected public provider.
        if self.mode == "public":
            provider = self._get_selected_public_provider(settings) or "groq"
            target = f"public:{provider}"
        else:
            target = "local"

        for i in range(self.assistant_combo.count()):
            if self.assistant_combo.itemData(i) == target:
                self.assistant_combo.setCurrentIndex(i)
                break

    def _get_selected_assistant(self) -> tuple[str, str | None]:
        data = str(self.assistant_combo.currentData() or "local")
        if data.startswith("public:"):
            return "public", data.split(":", 1)[1]
        return "local", None

    def _on_assistant_changed(self) -> None:
        mode, provider = self._get_selected_assistant()
        if mode == "local":
            self.setWindowTitle("Local Assistant")
        else:
            p = (provider or "public").strip().lower()
            if p == "openai":
                title_provider = "OpenAI"
            elif p == "groq":
                title_provider = "Groq"
            elif p == "gemini":
                title_provider = "Gemini"
            else:
                title_provider = (provider or "public").capitalize()
            self.setWindowTitle(f"Public Assistant ({title_provider})")

        # Persist selection for next app start
        try:
            data = str(self.assistant_combo.currentData() or "local")
            QSettings(ORG_NAME, APP_NAME).setValue(SETTINGS_ASSISTANT_SELECTED, data)
        except Exception:
            pass

        # Do not write anything into Output automatically.

    def _help_clicked(self) -> None:
        mode, provider = self._get_selected_assistant()
        if mode == "local":
            self._set_output(self._local_help_text())
            return
        self._set_output(self._public_help_text(provider))

    def _assistant_answer_md_enabled(self) -> bool:
        try:
            settings = QSettings(ORG_NAME, APP_NAME)
            return bool(settings.value(SETTINGS_ASSISTANT_ANSWER_MD, False, type=bool))
        except Exception:
            return False

    def _write_and_open_md_output(self, md_text: str) -> None:
        # Keep a persistent .md on disk.
        try:
            app_dir = _get_app_data_dir()
            Path(app_dir).mkdir(parents=True, exist_ok=True)
            out_path = str(Path(app_dir) / "assistant_output.md")
            with open(out_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(md_text or "")
            self._last_md_path = out_path
        except Exception:
            # Non-fatal: we still show output in the UI.
            pass
        finally:
            self._refresh_output_action_buttons()

    def _refresh_output_action_buttons(self) -> None:
        md = self._assistant_answer_md_enabled()
        has_file = bool(self._last_md_path)
        try:
            if hasattr(self, "act_open_md"):
                self.act_open_md.setEnabled(md and has_file)
            if hasattr(self, "act_save_md_as"):
                self.act_save_md_as.setEnabled(md)
        except Exception:
            pass

    def _sync_output_mode(self) -> None:
        md = self._assistant_answer_md_enabled()
        try:
            self.output_md.setVisible(md)
            self.output_text.setVisible(not md)
        except Exception:
            pass
        self._refresh_output_action_buttons()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        try:
            self.output_busy.setVisible(bool(busy))
            self.output_status_label.setText(message if busy else "")
            self.run_btn.setEnabled(not bool(busy))
            self.task_edit.setEnabled(not bool(busy))
            QApplication.processEvents()
        except Exception:
            pass

    def _current_output_plain_text(self) -> str:
        try:
            if self._assistant_answer_md_enabled():
                return self.output_md.toPlainText() or ""
            return self.output_text.toPlainText() or ""
        except Exception:
            return ""

    def _open_last_md_external(self) -> None:
        if not self._last_md_path:
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_md_path))
        except Exception:
            pass

    def _save_last_md_as(self) -> None:
        default_name = "assistant_output.md"
        if self._last_md_path:
            try:
                default_name = os.path.basename(self._last_md_path)
            except Exception:
                default_name = "assistant_output.md"

        path, _flt = QFileDialog.getSaveFileName(self, "Save Markdown", default_name, "Markdown (*.md);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self._current_output_plain_text())
        except Exception as e:
            QMessageBox.warning(self, "Save", f"Could not save: {e}")

    def _set_output(self, text: str) -> None:
        s = str(text or "")
        self._sync_output_mode()
        if self._assistant_answer_md_enabled():
            self._write_and_open_md_output(s)
            # Prefer rendered Markdown when available.
            try:
                if hasattr(self.output_md, "setMarkdown"):
                    self.output_md.setMarkdown(s)
                else:
                    self.output_md.setPlainText(s)
            except Exception:
                self.output_md.setPlainText(s)
            return

        self.output_text.setPlainText(s)

    @staticmethod
    def _public_help_text(provider: str | None) -> str:
        p = (provider or "").strip().lower() or "(auto)"
        return (
            "Public Assistant – nápověda:\n"
            f"- Provider: {p}\n"
            "- API klíč nastav v Settings → AI → Public\n"
            "- Podporováno: Groq, OpenAI, Gemini\n"
        )

    def _open_settings_clicked(self) -> None:
        if callable(self.open_settings):
            try:
                self.open_settings()
            except Exception as e:
                QMessageBox.warning(self, "Settings", f"Could not open settings: {e}")
        else:
            QMessageBox.information(self, "Settings", "Open Settings from the main window menu: Settings → Settings…")

    def _run_local(self, task: str, text: str) -> str:
        task_l = (task or "").strip().lower()

        if task_l in {"help", "?", "napoveda", "nápověda", "co umí", "co umi", "commands"}:
            return self._local_help_text()

        # Lightweight stopwords for scoring (CS + EN). Not exhaustive; tuned for robustness.
        stopwords = {
            # Czech
            "a", "aby", "ahoj", "aj", "ak", "ale", "ano", "asi", "aspoň", "az", "až",
            "bez", "bude", "budou", "by", "byl", "byla", "byli", "bylo", "být", "byť",
            "co", "což", "cz", "dnes", "do", "doc", "docs", "e", "email", "fakt", "hmm",
            "i", "jak", "jako", "je", "jeho", "jej", "její", "jejich", "jen", "ještě", "jsi", "jsme", "jsou", "jste",
            "k", "kam", "kde", "kdo", "kdy", "když", "ke", "která", "které", "který", "kteří",
            "kvůli", "li", "má", "mají", "mezi", "mít", "mně", "může", "můžou", "na", "nad", "ne", "nebo",
            "nej", "není", "nic", "nový", "nová", "nové", "noví", "o", "od", "on", "ona", "ono", "oni", "oproti",
            "po", "pod", "podle", "pokud", "proto", "protože", "pro", "před", "přes", "se", "si", "s", "stále",
            "tak", "taky", "takže", "ten", "tento", "tím", "to", "tohle", "tom", "tomu", "tu", "tuto", "tvůj", "tvoje",
            "u", "už", "v", "ve", "w", "www", "vy", "vás", "váš", "vaše", "vše", "však", "že",
            "z", "za", "ze",
            # English
            "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could",
            "did", "do", "does", "done", "for", "from", "had", "has", "have", "he", "her", "here", "him", "his",
            "how", "i", "if", "in", "into", "is", "it", "its", "just", "may", "me", "more", "most", "my",
            "no", "not", "of", "on", "or", "our", "out", "she", "so", "than", "that", "the", "their", "them",
            "then", "there", "these", "they", "this", "to", "too", "up", "us", "was", "we", "were", "what", "when",
            "where", "which", "who", "will", "with", "you", "your",
        }

        # Common helpers
        def _non_empty_lines(src: str) -> list[str]:
            return [ln.rstrip() for ln in src.splitlines() if ln.strip()]

        def _parse_int_from_task(default: int) -> int:
            m = re.search(r"\b(\d{1,3})\b", task_l)
            if not m:
                return default
            try:
                val = int(m.group(1))
                return val if val > 0 else default
            except Exception:
                return default

        def _split_sentences(src: str) -> list[str]:
            s = re.sub(r"\s+", " ", (src or "").strip())
            if not s:
                return []
            # Split on common sentence terminators. Keep it simple and language-agnostic.
            parts = re.split(r"(?<=[\.!\?])\s+", s)
            out = []
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                # Avoid treating short fragments as sentences
                if len(p) < 20 and not re.search(r"[A-Za-zÀ-ž]", p):
                    continue
                out.append(p)
            return out

        def _tokenize_words(src: str) -> list[str]:
            return [w.lower() for w in re.findall(r"[A-Za-zÀ-ž0-9]+", src or "")]

        def _extractive_summary(src: str, max_items: int, as_bullets: bool) -> str:
            sentences = _split_sentences(src)
            if not sentences:
                # Fallback to line-based if we can't split sentences.
                lines2 = _non_empty_lines(src)
                take = min(max_items, len(lines2))
                if take <= 0:
                    return "(Nothing to summarize.)"
                if as_bullets:
                    return "\n".join(f"- {lines2[i][:240]}" for i in range(take))
                return " ".join(lines2[:take])

            words = _tokenize_words(src)
            freq: dict[str, float] = {}
            for w in words:
                if len(w) <= 2:
                    continue
                if w in stopwords:
                    continue
                freq[w] = freq.get(w, 0.0) + 1.0
            if not freq:
                # If everything is stopwords, pick first sentences.
                take = min(max_items, len(sentences))
                picked = sentences[:take]
                return "\n".join(f"- {s}" for s in picked) if as_bullets else " ".join(picked)

            max_f = max(freq.values())
            for k in list(freq.keys()):
                freq[k] = freq[k] / max_f

            scored: list[tuple[float, int, str]] = []
            for idx, sent in enumerate(sentences):
                toks = _tokenize_words(sent)
                if not toks:
                    continue
                score = 0.0
                useful = 0
                for t in toks:
                    if len(t) <= 2 or t in stopwords:
                        continue
                    useful += 1
                    score += freq.get(t, 0.0)
                # Normalize and lightly penalize very long sentences
                if useful > 0:
                    score = score / useful
                score = score / (1.0 + max(0, (len(sent) - 220)) / 220.0)
                # Small boost for sentences that look like key statements
                if re.search(r"\b(důležité|hlavně|shrnutí|závěr|conclusion|key|important)\b", sent, re.IGNORECASE):
                    score *= 1.15
                scored.append((score, idx, sent))

            if not scored:
                take = min(max_items, len(sentences))
                picked = sentences[:take]
            else:
                take = min(max_items, len(scored))
                top = sorted(scored, key=lambda x: x[0], reverse=True)[:take]
                picked = [s for _, _, s in sorted(top, key=lambda x: x[1])]

            if as_bullets:
                return "\n".join(f"- {s}" for s in picked)
            return " ".join(picked)

        def _keywords(src: str, max_items: int) -> list[str]:
            toks = _tokenize_words(src)
            counts: dict[str, int] = {}
            for t in toks:
                if len(t) <= 3:
                    continue
                if t in stopwords:
                    continue
                counts[t] = counts.get(t, 0) + 1
            items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            return [w for w, _ in items[:max_items]]

        def _outline_from_headings(src: str, max_items: int) -> list[str]:
            out: list[str] = []
            for ln in (src or "").splitlines():
                s = ln.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    out.append(s.lstrip("#").strip())
                    continue
                # Simple heuristic: all-caps or ends with ':'
                if (len(s) >= 6 and s.upper() == s and re.search(r"[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", s)) or s.endswith(":"):
                    out.append(s.rstrip(":"))
            # Deduplicate while preserving order
            seen = set()
            uniq = []
            for item in out:
                if item in seen:
                    continue
                seen.add(item)
                uniq.append(item)
            return uniq[:max_items]

        # 1) Better summarization (extractive)
        lines = _non_empty_lines(text)
        if any(k in task_l for k in ["tldr", "tl;dr", "stručně", "strucne", "krátce", "kratce", "short summary"]):
            take = _parse_int_from_task(3)
            return _extractive_summary(text, max_items=take, as_bullets=True)

        if any(k in task_l for k in ["shrň", "shrnout", "summary", "summarize", "souhrn", "zhrň", "zhrnout"]):
            take = _parse_int_from_task(5)
            as_bullets = True
            if any(k in task_l for k in ["věty", "vety", "sentences", "odstavec", "paragraph"]):
                as_bullets = False
            return _extractive_summary(text, max_items=take, as_bullets=as_bullets)

        if any(k in task_l for k in ["odráž", "odraz", "body", "bullet", "bullets"]):
            take = _parse_int_from_task(8)
            return _extractive_summary(text, max_items=take, as_bullets=True)

        # 2) Cleanup / remove empty lines
        if any(k in task_l for k in ["vyčisti", "očisti", "ocisti", "cleanup", "trim"]):
            cleaned = "\n".join(ln.strip() for ln in text.splitlines())
            return cleaned

        if any(k in task_l for k in ["odstraň prázd", "odstran prazd", "remove empty", "drop empty"]):
            return "\n".join(_non_empty_lines(text))

        # 3) Basic stats
        if any(k in task_l for k in ["stat", "statistics", "počty", "pocty", "counts", "info"]):
            all_lines = text.splitlines()
            non_empty = _non_empty_lines(text)
            words = re.findall(r"\S+", text)
            return (
                f"Lines: {len(all_lines)}\n"
                f"Non-empty lines: {len(non_empty)}\n"
                f"Words: {len(words)}\n"
                f"Chars: {len(text)}"
            )

        # 4) Case conversions
        if any(k in task_l for k in ["uppercase", "upper", "velk", "velká", "velka"]):
            return text.upper()
        if any(k in task_l for k in ["lowercase", "lower", "mal", "malá", "mala"]):
            return text.lower()
        if any(k in task_l for k in ["titlecase", "title", "nadpis"]):
            return text.title()

        # 5) Sort / dedupe lines
        if any(k in task_l for k in ["seřaď", "serad", "sort"]):
            reverse = any(k in task_l for k in ["sestup", "desc", "reverse", "obrácen", "obracen"])
            sorted_lines = sorted(_non_empty_lines(text), key=lambda s: s.lower(), reverse=reverse)
            return "\n".join(sorted_lines)

        if any(k in task_l for k in ["unikátní", "unikatni", "dedupe", "unique"]):
            seen = set()
            out = []
            for ln in _non_empty_lines(text):
                key = ln
                if key in seen:
                    continue
                seen.add(key)
                out.append(ln)
            return "\n".join(out)

        # 6) Extract URLs / emails
        if any(k in task_l for k in ["url", "linky", "links", "odkazy"]):
            urls = re.findall(r"https?://[^\s\]\)\>\"']+", text)
            return "\n".join(urls) if urls else "(No URLs found.)"

        if any(k in task_l for k in ["email", "e-mail", "maily", "mail"]):
            emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
            return "\n".join(emails) if emails else "(No emails found.)"

        # 7) JSON pretty print
        if "json" in task_l and any(k in task_l for k in ["pretty", "format", "indent", "zformát", "zformat"]):
            try:
                obj = json.loads(text)
            except Exception as e:
                return f"Invalid JSON: {e}"
            return json.dumps(obj, ensure_ascii=False, indent=2)

        # 8) Find/replace
        # a) sed-like: s/old/new/
        m = re.search(r"\bs/(.*?)/(.*?)/\b", task, flags=re.IGNORECASE)
        if m:
            old, new = m.group(1), m.group(2)
            return text.replace(old, new)

        # b) replace "old" -> "new" or replace old -> new
        m = re.search(r"\breplace\s+(.+?)\s*(?:->|=>|with)\s*(.+)$", task, flags=re.IGNORECASE)
        if not m:
            m = re.search(r"\bnahrad\s+(.+?)\s*(?:->|=>|na)\s*(.+)$", task, flags=re.IGNORECASE)
        if m:
            old = m.group(1).strip().strip('"\'')
            new = m.group(2).strip().strip('"\'')
            if not old:
                return "Replace error: missing 'old' value."
            return text.replace(old, new)

        # 9) Keywords / outline
        if any(k in task_l for k in ["klíčová slova", "klicova slova", "keywords", "tagy", "tags"]):
            n = _parse_int_from_task(12)
            keys = _keywords(text, max_items=n)
            return "\n".join(f"- {k}" for k in keys) if keys else "(No keywords found.)"

        if any(k in task_l for k in ["osnova", "outline", "obsah", "headings", "nadpis"]):
            n = _parse_int_from_task(12)
            items = _outline_from_headings(text, max_items=n)
            return "\n".join(f"- {it}" for it in items) if items else "(No headings found.)"

        return self._local_help_text()

    @staticmethod
    def _local_help_text(task: str | None = None) -> str:
        base = (
            "Local Assistant (offline) – příkazy:\n"
            "- Nápověda: 'help' / 'nápověda'\n"
            "- Shrnutí: 'shrň 5' / 'summarize 5'\n"
            "- TL;DR: 'TL;DR 3' / 'stručně 3'\n"
            "- Odrážky: 'odrážky 8' / 'bullet points 8'\n"
            "- Vyčistit whitespace: 'vyčisti' / 'cleanup'\n"
            "- Odstranit prázdné řádky: 'remove empty lines'\n"
            "- Statistiky: 'stats' / 'počty'\n"
            "- Seřadit řádky: 'seřaď' / 'sort' (+ 'desc')\n"
            "- Unikátní řádky: 'unikátní' / 'dedupe'\n"
            "- Najít URL/email: 'url' / 'email'\n"
            "- Klíčová slova: 'klíčová slova 12' / 'keywords 12'\n"
            "- Osnova z nadpisů: 'osnova' / 'outline'\n"
            "- JSON pretty: 'json pretty'\n"
            "- Nahradit: 'replace A -> B' nebo 's/A/B/'\n"
        )
        return base

    def _run_public(self, task: str, editor_text: str, provider_override: str | None = None) -> None:
        settings = QSettings(ORG_NAME, APP_NAME)

        provider = (provider_override or "").strip().lower() or self._get_selected_public_provider(settings)
        
        if not provider:
            self._set_output("No public AI provider selected.")
            return

        api_key = ""
        if provider == "groq":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_GROQ_API_KEY, "") or "")
            api_key = _dpapi_decrypt_from_b64(encrypted).strip()
        elif provider == "openai":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_OPENAI_API_KEY, "") or "")
            api_key = _dpapi_decrypt_from_b64(encrypted).strip()
        elif provider == "gemini":
            encrypted = str(settings.value(SETTINGS_AI_PUBLIC_GEMINI_API_KEY, "") or "")
            api_key = _dpapi_decrypt_from_b64(encrypted).strip()
        else:
            self._set_output("No supported public AI provider selected.")
            return

        if not api_key:
            self._set_output(
                f"Missing API key for {provider}.\n"
                "Go to Settings -> AI -> Public and enter the API key."
            )
            return

        prompt = self._build_public_prompt(task=task, editor_text=editor_text)

        self.setCursor(Qt.WaitCursor)
        self._set_busy(True, "Generating…")
        try:
            answer = ""
            if provider == "groq":
                answer = self._groq_generate(api_key=api_key, prompt=prompt)
            elif provider == "openai":
                answer = self._openai_generate(api_key=api_key, prompt=prompt)
            elif provider == "gemini":
                answer = self._gemini_generate(api_key=api_key, prompt=prompt)
            else:
                answer = f"Provider '{provider}' is not fully implemented yet."

            self._set_output(self._clean_public_answer(answer))
        except Exception as e:
            self._set_output(f"Public AI error: {e}")
        finally:
            self._set_busy(False)
            self.setCursor(Qt.ArrowCursor)

    def _get_selected_public_provider(self, settings: QSettings):
        if bool(settings.value(SETTINGS_AI_PUBLIC_GROQ_ENABLED, False, type=bool)):
            return "groq"
        if bool(settings.value(SETTINGS_AI_PUBLIC_OPENAI_ENABLED, False, type=bool)):
            return "openai"
        if bool(settings.value(SETTINGS_AI_PUBLIC_GEMINI_ENABLED, False, type=bool)):
            return "gemini"
        return None

    def _build_public_prompt(self, task: str, editor_text: str) -> str:
        text = (editor_text or "").strip()
        rules = (
            "RULES:\n"
            "- Output ONLY the final result.\n"
            "- Do NOT restate or echo the user task/instructions.\n"
            "- Do NOT include labels like 'Task:' / 'Úkol:' / 'Instruction:'.\n"
            "- Do NOT mention these rules.\n\n"
        )
        if text:
            return (
                rules
                + "User instruction:\n"
                + str(task)
                + "\n\nInput text:\n"
                + text
            )
        return rules + "User instruction:\n" + str(task)

    @staticmethod
    def _clean_public_answer(answer: str) -> str:
        # Some models still echo the instruction. Strip obvious leading label blocks.
        s = (answer or "").strip()
        if not s:
            return ""

        lines = s.splitlines()
        # Drop leading empty lines
        while lines and not lines[0].strip():
            lines.pop(0)

        if not lines:
            return ""

        # Remove a leading "Task:/Úkol:/Instruction:" line (and a following blank line)
        first = lines[0].strip().lower()
        if first.startswith("task:") or first.startswith("úkol:") or first.startswith("ukol:") or first.startswith("instruction:"):
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)

        return "\n".join(lines).strip()

    def _groq_select_model(self, api_key: str) -> str:
        # Prefer a stable, commonly-available chat model. If Groq changes model names,
        # we fall back to listing available models and picking the best match.
        preferences = [
            "llama-3.1-8b-instant",
            "llama-3.1-70b-versatile",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ]

        url = "https://api.groq.com/openai/v1/models"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            # If listing fails (network, auth, etc.), use the top preference.
            return preferences[0]

        try:
            body = json.loads(raw)
        except Exception:
            return preferences[0]

        data = body.get("data") or []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        for pref in preferences:
            if pref in ids:
                return pref
        return ids[0] if ids else preferences[0]

    def _groq_generate(self, api_key: str, prompt: str) -> str:
        # Cache the selected model for this dialog instance to avoid extra round-trips.
        model = getattr(self, "_groq_model", "") or self._groq_select_model(api_key)
        self._groq_model = model
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0"
            },
        )

        def _do_request() -> str:
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                raw_err = e.read().decode("utf-8", errors="replace")
                raise urllib.error.HTTPError(e.url, e.code, raw_err, e.hdrs, e.fp)

        try:
            raw = _do_request()
        except urllib.error.HTTPError as e:
            raw_err = str(e)
            # If the model is deprecated, pick a new model and retry once.
            try:
                body_err = json.loads(raw_err)
                err = (body_err.get("error") or {}) if isinstance(body_err, dict) else {}
                if err.get("code") == "model_decommissioned":
                    self._groq_model = self._groq_select_model(api_key)
                    payload["model"] = self._groq_model
                    data_retry = json.dumps(payload).encode("utf-8")
                    req.data = data_retry
                    raw = _do_request()
                else:
                    raise RuntimeError(f"HTTP {e.code}: {raw_err}")
            except Exception:
                raise RuntimeError(f"HTTP {e.code}: {raw_err}")

        body = json.loads(raw)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("No choices in response")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""

        if not content:
            raise RuntimeError("Empty response content")

        return content

    def _openai_select_model(self, api_key: str) -> str:
        preferences = [
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1-mini",
            "gpt-4.1",
        ]

        url = "https://api.openai.com/v1/models"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return preferences[0]

        try:
            body = json.loads(raw)
        except Exception:
            return preferences[0]

        data = body.get("data") or []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        for pref in preferences:
            if pref in ids:
                return pref
        return ids[0] if ids else preferences[0]

    def _openai_generate(self, api_key: str, prompt: str) -> str:
        model = getattr(self, "_openai_model", "") or self._openai_select_model(api_key)
        self._openai_model = model

        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")

            # Try to extract structured OpenAI error details
            try:
                body_err = json.loads(raw_err)
            except Exception:
                body_err = None

            code = None
            msg = None
            if isinstance(body_err, dict):
                err = body_err.get("error")
                if isinstance(err, dict):
                    code = err.get("code")
                    msg = err.get("message")

            if e.code == 429 and code == "insufficient_quota":
                raise RuntimeError(
                    "OpenAI odpovědělo 429: insufficient_quota – API key je valid, ale účet/organizace nemá aktivní billing nebo nemá dostupnou kvótu.\n\n"
                    "Co zkontrolovat:\n"
                    "- OpenAI Platform → Billing: zapnout platby / přidat platební metodu\n"
                    "- Jestli klíč patří do správného projektu/organizace\n"
                    "- Nastavené limity (monthly hard limit / budget)\n\n"
                    "Detail od API:\n"
                    + (msg or raw_err)
                )

            if e.code == 429 and code == "rate_limit_exceeded":
                raise RuntimeError(
                    "OpenAI odpovědělo 429: rate_limit_exceeded – příliš mnoho požadavků. Zkus to za chvíli, nebo sniž frekvenci/velikost promptu.\n\n"
                    "Detail od API:\n"
                    + (msg or raw_err)
                )

            raise RuntimeError(f"HTTP {e.code}: {raw_err}")

        body = json.loads(raw)
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("No choices in response")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not content:
            raise RuntimeError("Empty response content")
        return content

    def _gemini_select_model(self, api_key: str) -> str:
        # Prefer a fast, cheap model; fall back if listing fails.
        preferences = [
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]

        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={urllib.parse.quote(api_key)}"
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return preferences[0]

        try:
            body = json.loads(raw)
        except Exception:
            return preferences[0]

        models = body.get("models") or []
        names = []
        for m in models:
            if not isinstance(m, dict):
                continue
            name = m.get("name")
            if isinstance(name, str) and name.startswith("models/"):
                names.append(name.split("/", 1)[1])

        for pref in preferences:
            if pref in names:
                return pref
        return names[0] if names else preferences[0]

    def _gemini_generate(self, api_key: str, prompt: str) -> str:
        model = getattr(self, "_gemini_model", "") or self._gemini_select_model(api_key)
        self._gemini_model = model

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]}
            ],
            "generationConfig": {
                "temperature": 0.7,
            },
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8", errors="replace")
            # Gemini errors are typically JSON with {"error": {"message": ...}}
            try:
                body_err = json.loads(raw_err)
            except Exception:
                body_err = None
            msg = None
            if isinstance(body_err, dict):
                err = body_err.get("error")
                if isinstance(err, dict):
                    msg = err.get("message")
            raise RuntimeError(f"HTTP {e.code}: {msg or raw_err}")

        body = json.loads(raw)
        candidates = body.get("candidates") or []
        if not candidates:
            raise RuntimeError("No candidates in response")

        content = (candidates[0].get("content") or {}) if isinstance(candidates[0], dict) else {}
        parts = content.get("parts") or []
        if not parts:
            raise RuntimeError("Empty response content")

        # parts can contain multiple chunks; concatenate text parts
        out = []
        for p in parts:
            if isinstance(p, dict) and p.get("text"):
                out.append(str(p.get("text")))
        text_out = "".join(out).strip()
        if not text_out:
            raise RuntimeError("Empty response text")
        return text_out


class _OcrOnceController(QObject):
    """Runs OCR for one payload using the existing persistent OCR subprocess worker."""

    ocr_request = Signal(object)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _OcrSubprocessWorker()
        self._worker.moveToThread(self._thread)
        self.ocr_request.connect(self._worker.run)
        self._worker.finished.connect(self.finished)
        self._worker.error.connect(self.error)
        self._thread.start()

    def shutdown(self) -> None:
        try:
            self._worker.shutdown()
        except Exception:
            pass
        try:
            if self._thread.isRunning():
                self._thread.quit()
                self._thread.wait(1500)
        except Exception:
            pass


class OcrService(QObject):
    """Single shared OCR service for the whole app.

    PaddleOCR 3.x cold start can take minutes (imports + model init). If we start a new OCR server
    per capture/window, the user experiences "minutes" every time. This service keeps exactly one
    long-lived OCR server process and reuses it across Editor/Assistant flows.

    Requests are serialized (one at a time) via a small FIFO queue.
    """

    _request_signal = Signal(object)
    _request_windows_signal = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._paddle_worker = _OcrSubprocessWorker()
        self._paddle_worker.moveToThread(self._thread)
        self._request_signal.connect(self._paddle_worker.run)
        self._paddle_worker.finished.connect(self._on_finished_text)
        self._paddle_worker.finished_detail.connect(self._on_finished_detail)
        self._paddle_worker.error.connect(self._on_error)

        self._windows_worker = _WindowsOcrWorker()
        self._windows_worker.moveToThread(self._thread)
        self._request_windows_signal.connect(self._windows_worker.run)
        self._windows_worker.finished.connect(self._on_finished_text)
        self._windows_worker.finished_detail.connect(self._on_finished_detail)
        self._windows_worker.error.connect(self._on_error)
        self._thread.start()

        self._busy = False
        self._queue: list[tuple[dict, object, object, bool]] = []
        self._current_ok = None
        self._current_err = None
        self._current_want_detail = False

    def submit(self, payload: dict, on_ok, on_err) -> None:
        if not isinstance(payload, dict):
            try:
                on_err("OCR Error: Invalid payload")
            except Exception:
                pass
            return

        want_detail = bool(payload.get("return_boxes", False))
        self._queue.append((payload, on_ok, on_err, want_detail))
        self._pump()

    def _pump(self) -> None:
        if self._busy:
            return
        if not self._queue:
            return
        payload, on_ok, on_err, want_detail = self._queue.pop(0)
        self._busy = True
        self._current_ok = on_ok
        self._current_err = on_err
        self._current_want_detail = bool(want_detail)

        engine = str((payload.get("engine") if isinstance(payload, dict) else "") or "paddle").strip().lower()
        if engine == "windows":
            self._request_windows_signal.emit(payload)
        else:
            self._request_signal.emit(payload)

    def _on_finished_text(self, text: str) -> None:
        cb = self._current_ok
        self._busy = False
        self._current_ok = None
        self._current_err = None
        self._current_want_detail = False
        try:
            if callable(cb):
                cb(text)
        finally:
            self._pump()

    def _on_finished_detail(self, resp: object) -> None:
        cb = self._current_ok
        self._busy = False
        self._current_ok = None
        self._current_err = None
        self._current_want_detail = False
        try:
            if callable(cb):
                cb(resp)
        finally:
            self._pump()

    def _on_error(self, message: str) -> None:
        cb = self._current_err
        self._busy = False
        self._current_ok = None
        self._current_err = None
        self._current_want_detail = False
        try:
            if callable(cb):
                cb(message)
        finally:
            self._pump()

    def shutdown(self) -> None:
        try:
            self._paddle_worker.shutdown()
        except Exception:
            pass
        try:
            if self._thread.isRunning():
                self._thread.quit()
                self._thread.wait(1500)
        except Exception:
            pass


class OcrAssistantFlow(QObject):
    """Capture -> OCR -> open Assistant (skip EditorWindow UI)."""

    def __init__(self, tray_app, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.tray_app = tray_app
        self.pixmap = pixmap
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self._source_text = ""
        self._temp_path = None

        self.dialog = AssistantDialog(
            mode="local",
            get_editor_text=lambda: self._source_text,
            set_editor_text=None,
            open_settings=lambda: self.tray_app.show_settings("general"),
            parent=None,
        )
        try:
            self.dialog._set_output("Running OCR…")
        except Exception:
            pass
        self.dialog.set_run_enabled(False)

        self._ocr_service = getattr(self.tray_app, "ocr_service", None)
        self.dialog.destroyed.connect(lambda *_: self.shutdown())

    def show(self) -> None:
        self.dialog.show()
        try:
            self.dialog.raise_()
            self.dialog.activateWindow()
            self.dialog.task_edit.setFocus()
        except Exception:
            pass
        QTimer.singleShot(0, self.start_ocr)

    def shutdown(self) -> None:
        self._ocr_service = None
        self._cleanup_temp()

    def _cleanup_temp(self) -> None:
        temp_path = getattr(self, "_temp_path", None)
        self._temp_path = None
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _compute_ocr_scale(self) -> int:
        use_auto_scale = bool(self.settings.value(SETTINGS_OCR_SCALE_AUTO, True, type=bool))
        try:
            scale = int(self.settings.value(SETTINGS_OCR_SCALE, 1) or 1)
        except Exception:
            scale = 1

        if use_auto_scale:
            try:
                w = int(self.pixmap.width())
                h = int(self.pixmap.height())
            except Exception:
                w, h = 0, 0
            area = w * h
            if area <= 0:
                scale = 2
            elif area < 700_000:
                scale = 3
            elif area < 2_200_000:
                scale = 2
            else:
                scale = 1

        return max(1, min(int(scale), 4))

    def start_ocr(self) -> None:
        # Always auto-run OCR in this flow.
        try:
            self.dialog._set_output("Running OCR…")
            self.dialog.set_run_enabled(False)
        except Exception:
            pass

        use_in_memory = bool(self.settings.value(SETTINGS_OCR_IN_MEMORY, False, type=bool))
        use_greedy = bool(self.settings.value(SETTINGS_OCR_USE_GREEDY, True, type=bool))
        scale = self._compute_ocr_scale()

        default_engine = "windows" if _windows_ocr_available() else "paddle"
        engine = str(self.settings.value(SETTINGS_OCR_ENGINE, default_engine) or default_engine).strip().lower()
        if engine not in ("windows", "paddle"):
            engine = default_engine

        payload = {
            "engine": engine,
            "use_greedy": use_greedy,
            "image_path": None,
            "png_b64": None,
            "scale": scale,
        }

        self._cleanup_temp()

        if use_in_memory:
            buf = QBuffer()
            buf.open(QBuffer.ReadWrite)
            ok = self.pixmap.save(buf, "PNG")
            if not ok:
                self._on_ocr_error("OCR Error: Could not encode image.")
                return
            payload["png_b64"] = base64.b64encode(bytes(buf.data())).decode("ascii")
            buf.close()
        else:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                self._temp_path = f.name
            self.pixmap.save(self._temp_path)
            if not self._temp_path or not os.path.exists(self._temp_path) or os.path.getsize(self._temp_path) == 0:
                self._on_ocr_error("OCR Error: Could not save image.")
                return
            payload["image_path"] = self._temp_path

        svc = self._ocr_service
        if svc is None:
            self._on_ocr_error("OCR Error: OCR service is not available.")
            return
        svc.submit(payload, self._on_ocr_finished, self._on_ocr_error)

    def _on_ocr_finished(self, out_text: str) -> None:
        self._source_text = (out_text or "").strip()
        self._cleanup_temp()

        if self._source_text:
            self.dialog._set_output("OCR ready. Enter task and press Run.")
        else:
            self.dialog._set_output("OCR finished: no text detected. You can still ask a question.")

        self.dialog.set_run_enabled(True)
        try:
            self.dialog.task_edit.setFocus()
        except Exception:
            pass

    def _on_ocr_error(self, message: str) -> None:
        self._cleanup_temp()
        self._source_text = ""
        self.dialog._set_output(str(message or "OCR failed"))
        # Still allow using Assistant for unrelated questions.
        self.dialog.set_run_enabled(True)
        try:
            self.dialog.task_edit.setFocus()
        except Exception:
            pass


class EditorWindow(QWidget):
    enhance_request = Signal(str)

    def __init__(self, pixmap: QPixmap, tray_app=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Editor")
        self.setWindowIcon(QIcon(resource_path("ico.ico")))
        # Ensure the QWidget is deleted when closed (prevents keeping heavy resources alive)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(800, 600)
        
        self.original_pixmap = pixmap
        self.tray_app = tray_app
        self.settings_dialog = None
        
        layout = QVBoxLayout()
        
        # --- Menu Bar ---
        menu_bar = QMenuBar(self)
        file_menu = menu_bar.addMenu("File")
        
        # Save
        save_action = QAction("Save Image...", self)
        save_action.setShortcut(QKeySequence.Save)
        save_action.triggered.connect(self.save_image)
        file_menu.addAction(save_action)
        
        save_txt_action = QAction("Save as TXT", self)
        save_txt_action.triggered.connect(self.save_text_as_txt)
        file_menu.addAction(save_txt_action)

        # Copy image (no Ctrl+C, that belongs to text)
        copy_action = QAction("Copy image", self)
        copy_action.triggered.connect(self.copy_to_clipboard)
        file_menu.addAction(copy_action)
        
        file_menu.addSeparator()
        
        # Exit
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Tools Menu
        tools_menu = menu_bar.addMenu("Tools")

        # Edit Menu
        edit_menu = menu_bar.addMenu("Edit")

        copy_text_action = QAction("Copy", self)
        copy_text_action.setShortcut(QKeySequence.Copy)
        # Make sure Ctrl+C works even if focus/menus behave oddly on QWidget+QMenuBar.
        copy_text_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        copy_text_action.triggered.connect(self.copy_text_to_clipboard)
        self.addAction(copy_text_action)
        edit_menu.addAction(copy_text_action)

        undo_action = QAction("Undo", self)
        undo_action.setShortcut(QKeySequence.Undo)  # Ctrl+Z
        undo_action.triggered.connect(lambda: self.text_edit.undo())
        edit_menu.addAction(undo_action)

        redo_action = QAction("Redo", self)
        redo_action.setShortcut(QKeySequence.Redo)  # Ctrl+Y
        redo_action.triggered.connect(lambda: self.text_edit.redo())
        edit_menu.addAction(redo_action)

        clear_action = QAction("Clear", self)
        clear_action.triggered.connect(self.clear_text_confirm)
        edit_menu.addAction(clear_action)

        # AI Tools Menu
        ai_menu = menu_bar.addMenu("AI Tools")

        # Settings Menu
        settings_menu = menu_bar.addMenu("Settings")

        editor_settings_action = QAction("Settings...", self)
        editor_settings_action.triggered.connect(self.open_settings)
        settings_menu.addAction(editor_settings_action)
        
        ocr_action = QAction("Extract Text (OCR)", self)
        ocr_action.triggered.connect(self.run_ocr)
        tools_menu.addAction(ocr_action)

        assistant_action = QAction("Assistant...", self)
        assistant_action.triggered.connect(self.open_assistant)
        ai_menu.addAction(assistant_action)

        enhance_action = QAction("Enhance image...", self)
        enhance_action.triggered.connect(self.enhance_image)
        tools_menu.addAction(enhance_action)
        
        layout.setMenuBar(menu_bar)
        
        # --- Splitter (Image + Text) ---
        self.splitter = QSplitter(Qt.Vertical)
        
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        # Allow shrinking below implicit size
        self.image_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        # self.image_label.setPixmap(pixmap) 
        
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Notes...")
        # Explicitly bind undo/redo shortcuts (QTextEdit usually has them, but keep consistent)
        self.text_edit.setUndoRedoEnabled(True)
        
        self.splitter.addWidget(self.image_label)
        self.splitter.addWidget(self.text_edit)
        
        # Restore splitter state/sizes
        self.settings = QSettings(ORG_NAME, APP_NAME)
        saved_state = self.settings.value("editor_splitter_state")
        if saved_state:
            self.splitter.restoreState(saved_state)
        
        layout.addWidget(self.splitter)
        self.setLayout(layout)
        
        # Install event filter to handle image resizing when splitter moves
        self.image_label.installEventFilter(self)

        self._assistant_dialog = None

        # OCR service: prefer shared tray-level service to avoid cold-start on each new Editor window.
        self._ocr_service = None
        if self.tray_app is not None and getattr(self.tray_app, "ocr_service", None) is not None:
            self._ocr_service = self.tray_app.ocr_service
        else:
            # Fallback for standalone EditorWindow usage
            self._ocr_service = OcrService(parent=self)

        self._ocr_progress = None
        self._ocr_temp_path = None

        # Enhance worker thread
        self._enhance_thread = QThread(self)
        self._enhance_worker = _EnhanceWorker()
        self._enhance_worker.moveToThread(self._enhance_thread)
        self.enhance_request.connect(self._enhance_worker.run)
        self._enhance_worker.finished.connect(self._on_enhance_finished)
        self._enhance_worker.error.connect(self._on_enhance_error)
        self._enhance_thread.start()

        self._enhance_progress = None
        self._enhance_temp_path = None

    def copy_text_to_clipboard(self) -> None:
        # QTextEdit.copy() copies only selection; if user has no selection,
        # copy the whole text to match common expectations.
        try:
            cursor = self.text_edit.textCursor()
            if cursor is not None and cursor.hasSelection():
                self.text_edit.copy()
            else:
                (QApplication.clipboard()).setText(self.text_edit.toPlainText() or "")
        except Exception:
            # Fallback: best-effort
            try:
                (QApplication.clipboard()).setText(self.text_edit.toPlainText() or "")
            except Exception:
                pass

    def open_settings(self):
        if self.tray_app and hasattr(self.tray_app, "show_settings"):
            self.tray_app.show_settings("general")
            return

        if not self.settings_dialog:
            settings = QSettings(ORG_NAME, APP_NAME)
            self.settings_dialog = SettingsDialog(settings=settings, tray_app=None, parent=self)

        self.settings_dialog.show_tab("general")
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def eventFilter(self, source, event):
        if source == self.image_label and event.type() == QEvent.Resize:
            self.update_image_display()
        return super().eventFilter(source, event)

    def update_image_display(self):
        if not self.original_pixmap.isNull() and self.image_label.size().isValid():
            w = self.image_label.width()
            h = self.image_label.height()
            if w > 0 and h > 0:
                scaled = self.original_pixmap.scaled(
                    w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                self.image_label.setPixmap(scaled)

    def closeEvent(self, event):
        # Save splitter state
        self.settings.setValue("editor_splitter_state", self.splitter.saveState())
        # Only shutdown local service (shared TrayApp service must stay alive).
        try:
            if self._ocr_service is not None and (self.tray_app is None or getattr(self.tray_app, "ocr_service", None) is None):
                self._ocr_service.shutdown()
        except Exception:
            pass
        self._ocr_service = None

        try:
            if hasattr(self, "_enhance_thread") and self._enhance_thread and self._enhance_thread.isRunning():
                self._enhance_thread.quit()
                self._enhance_thread.wait(1500)
        except Exception:
            pass

        # Drop TrayApp's reference so this window can be garbage-collected.
        try:
            if self.tray_app is not None and getattr(self.tray_app, "editor_window", None) is self:
                self.tray_app.editor_window = None
        except Exception:
            pass
        super().closeEvent(event)

    def save_image(self):
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Save Image",
            os.path.join(os.path.expanduser("~"), "Pictures", "screenshot.png"),
            "Images (*.png *.jpg *.bmp);;All Files (*)"
        )
        if file_name:
            self.original_pixmap.save(file_name)

    def _get_editor_text_for_assistant(self) -> str:
        return self.text_edit.toPlainText()

    def _set_editor_text_for_assistant(self, text: str) -> None:
        self.text_edit.setPlainText(text)

    def open_assistant(self):
        if not self._assistant_dialog:
            # Single dialog that can switch between Local/Public via the dropdown.
            self._assistant_dialog = AssistantDialog(
                mode="local",
                get_editor_text=self._get_editor_text_for_assistant,
                set_editor_text=self._set_editor_text_for_assistant,
                open_settings=self.open_settings,
                parent=self,
            )
        self._assistant_dialog.show()
        self._assistant_dialog.raise_()
        self._assistant_dialog.activateWindow()

    def run_ocr(self):
        if self._ocr_progress is not None:
            return

        self.setCursor(Qt.WaitCursor)
        QApplication.processEvents()

        temp_path = None
        try:
            use_in_memory = bool(self.settings.value(SETTINGS_OCR_IN_MEMORY, False, type=bool))
            use_greedy = bool(self.settings.value(SETTINGS_OCR_USE_GREEDY, True, type=bool))

            default_engine = "windows" if _windows_ocr_available() else "paddle"
            engine = str(self.settings.value(SETTINGS_OCR_ENGINE, default_engine) or default_engine).strip().lower()
            if engine not in ("windows", "paddle"):
                engine = default_engine

            use_auto_scale = bool(self.settings.value(SETTINGS_OCR_SCALE_AUTO, True, type=bool))

            try:
                scale = int(self.settings.value(SETTINGS_OCR_SCALE, 1) or 1)
            except Exception:
                scale = 1

            if use_auto_scale:
                # Simple heuristic: larger selections benefit from speed (lower scale),
                # small selections benefit from accuracy (higher scale).
                try:
                    w = int(self.original_pixmap.width())
                    h = int(self.original_pixmap.height())
                except Exception:
                    w, h = 0, 0
                area = w * h
                if area <= 0:
                    scale = 2
                elif area < 700_000:  # e.g. ~1000x700
                    scale = 3
                elif area < 2_200_000:  # e.g. ~1920x1080
                    scale = 2
                else:
                    scale = 1

            scale = max(1, min(scale, 4))

            payload = {
                "engine": engine,
                "use_greedy": use_greedy,
                "image_path": None,
                "png_b64": None,
                "scale": scale,
            }

            if use_in_memory:
                buf = QBuffer()
                buf.open(QBuffer.ReadWrite)
                ok = self.original_pixmap.save(buf, "PNG")
                if not ok:
                    self.text_edit.append("Error: Could not encode image.")
                    return
                payload["png_b64"] = base64.b64encode(bytes(buf.data())).decode("ascii")
                buf.close()
                self._ocr_temp_path = None
            else:
                # Save current image to temp file for OCR
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    temp_path = f.name

                self.original_pixmap.save(temp_path)
                if os.path.getsize(temp_path) == 0:
                    self.text_edit.append("Error: Saved image file is empty.")
                    return

                self._ocr_temp_path = temp_path
                payload["image_path"] = temp_path

            # Progress window (indeterminate)
            dlg = QProgressDialog("Running OCR…", "", 0, 0, self)
            dlg.setWindowTitle("OCR")
            dlg.setWindowModality(Qt.WindowModal)
            dlg.setCancelButton(None)
            dlg.setMinimumDuration(0)
            dlg.show()
            self._ocr_progress = dlg

            # Kick off OCR in worker thread
            svc = self._ocr_service
            if svc is None:
                self.text_edit.append("OCR Error: OCR service is not available.")
                self._cleanup_ocr_ui_and_temp()
                return
            svc.submit(payload, self._on_ocr_finished, self._on_ocr_error)
        except Exception as e:
            error_msg = f"OCR Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
            self.text_edit.append(error_msg)
            print(error_msg)
            self._cleanup_ocr_ui_and_temp()

    def _cleanup_ocr_ui_and_temp(self) -> None:
        try:
            if self._ocr_progress is not None:
                self._ocr_progress.close()
        except Exception:
            pass
        self._ocr_progress = None
        self.setCursor(Qt.ArrowCursor)

        temp_path = getattr(self, "_ocr_temp_path", None)
        self._ocr_temp_path = None
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _on_ocr_finished(self, out_text: str) -> None:
        try:
            if out_text:
                self.text_edit.append(out_text)
            else:
                self.text_edit.append("No text detected.")
        finally:
            self._cleanup_ocr_ui_and_temp()

    def _on_ocr_error(self, message: str) -> None:
        try:
            # Keep current behavior: append detailed error
            self.text_edit.append(message)
            print(message)
        finally:
            self._cleanup_ocr_ui_and_temp()

    def enhance_image(self) -> None:
        if self._enhance_progress is not None:
            return

        self.setCursor(Qt.WaitCursor)
        QApplication.processEvents()

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                temp_path = f.name

            self.original_pixmap.save(temp_path)
            if os.path.getsize(temp_path) == 0:
                QMessageBox.warning(self, "Enhance", "Error: Saved image file is empty.")
                return

            self._enhance_temp_path = temp_path

            dlg = QProgressDialog("Enhancing image…", "", 0, 0, self)
            dlg.setWindowTitle("Enhance")
            dlg.setWindowModality(Qt.WindowModal)
            dlg.setCancelButton(None)
            dlg.setMinimumDuration(0)
            dlg.show()
            self._enhance_progress = dlg

            self.enhance_request.emit(temp_path)
        except Exception as e:
            msg = f"Enhance Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
            QMessageBox.warning(self, "Enhance", msg)
            self._cleanup_enhance_ui_and_temp()

    def _cleanup_enhance_ui_and_temp(self) -> None:
        try:
            if self._enhance_progress is not None:
                self._enhance_progress.close()
        except Exception:
            pass
        self._enhance_progress = None
        self.setCursor(Qt.ArrowCursor)

        temp_path = getattr(self, "_enhance_temp_path", None)
        self._enhance_temp_path = None
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _on_enhance_finished(self, png_bytes: bytes) -> None:
        try:
            pix = QPixmap()
            ok = pix.loadFromData(png_bytes, "PNG")
            if not ok or pix.isNull():
                QMessageBox.warning(self, "Enhance", "Enhance finished, but image could not be loaded.")
                return

            self.original_pixmap = pix
            self.update_image_display()
        finally:
            self._cleanup_enhance_ui_and_temp()

    def _on_enhance_error(self, message: str) -> None:
        try:
            QMessageBox.warning(self, "Enhance", message)
            print(message)
        finally:
            self._cleanup_enhance_ui_and_temp()

    def copy_to_clipboard(self):
        clipboard = QApplication.clipboard()
        clipboard.setPixmap(self.original_pixmap)

    def clear_text_confirm(self) -> None:
        answer = QMessageBox.question(
            self,
            "Clear",
            "Opravdu chceš vymazat text v editoru?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.text_edit.clear()

    def save_text_as_txt(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save as TXT",
            os.path.join(os.path.expanduser("~"), "Documents", "text.txt"),
            "Text files (*.txt);;All Files (*)",
        )
        if not path:
            return
        try:
            text = self.text_edit.toPlainText() or ""
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except Exception as e:
            QMessageBox.warning(self, "Save as TXT", f"Could not save file: {e}")

        
    def showEvent(self, event):
        # Force initial update
        self.update_image_display()
        super().showEvent(event)


class SnippingOverlay(QWidget):
    capture_taken = Signal(QPixmap)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_DeleteOnClose)
        
        # Capture full screen (all monitors)
        screens = QApplication.screens()
        
        # Determine total geometry of all monitors
        total_rect = QRect()
        for screen in screens:
            total_rect = total_rect.united(screen.geometry())
        self.total_rect = total_rect
            
        # Determine largest pixel ratio (scale factor)
        self.max_dpr = max(s.devicePixelRatio() for s in screens) if screens else 1.0
        
        # Store screen captures and their layout info
        self.captures = []
        
        # Sort screens by logical X position to determine physical layout order
        # This helps in reconstructing a continuous physical image
        sorted_screens = sorted(screens, key=lambda s: (s.geometry().y(), s.geometry().x()))
        
        current_phys_x = 0
        max_phys_y = 0

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Monitor detection:")
        for i, screen in enumerate(sorted_screens):
            geo = screen.geometry()
            dpr = screen.devicePixelRatio()
            print(f"  Monitor {i}: Logical geom={geo.getRect()}, DPR={dpr}")
        
        for screen in sorted_screens:
            # Grab raw physical pixels
            pix = screen.grabWindow(0)
            
            # Set DPR for pixmap to match monitor
            # This ensures logical size of pixmap matches logical geometry of window
            pix.setDevicePixelRatio(screen.devicePixelRatio())
            
            # Logical geometry relative to the overlay window
            log_rect = screen.geometry()
            rel_rect = QRect(
                log_rect.x() - total_rect.x(),
                log_rect.y() - total_rect.y(),
                log_rect.width(),
                log_rect.height()
            )
            
            # Estimate physical origin based on flow
            phys_rect = QRect(current_phys_x, 0, pix.width(), pix.height())
            
            self.captures.append({
                'pixmap': pix,
                'log_rect': rel_rect,   # For painting on screen (visual)
                'phys_rect': phys_rect, # For stitching info (backup)
                'screen': screen
            })
            
            # For physical offset shift use physical width (width * DPR if using logical, but pix.width() is raw)
            # pix.width() returns logical width if DPR is set!
            # Must get raw size
            raw_width = int(pix.width() * pix.devicePixelRatio())
            current_phys_x += raw_width
            
            raw_height = int(pix.height() * pix.devicePixelRatio())
            max_phys_y = max(max_phys_y, raw_height)

        # Determine total physical canvas size
        self.phys_total_w = current_phys_x
        self.phys_total_h = max_phys_y
        
        self.setCursor(QCursor(Qt.CrossCursor))
        
        self.start_pos = None
        self.end_pos = None
        self.is_selecting = False
        
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.start_pos = event.position().toPoint()
            self.is_selecting = True
            self.end_pos = self.start_pos
            self.update()
        event.accept()
    
    def mouseMoveEvent(self, event):  # noqa: N802
        if self.is_selecting:
            self.end_pos = event.position().toPoint()
            self.update()
        event.accept()
    
    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self.is_selecting:
            self.is_selecting = False
            self.end_pos = event.position().toPoint()
            
            selection_rect = QRect(self.start_pos, self.end_pos).normalized()
            
            if selection_rect.width() > 10 and selection_rect.height() > 10:
                self.process_capture(selection_rect)
                self.close()
            else:
                self.update()
        event.accept()
        
    def process_capture(self, selection_rect, emit_signal: bool = True):
        final_fragments = []
        total_output_width = 0
        max_output_height = 0
        
        # Sort captures by visual X order again to ensure left-to-right stitching
        sorted_captures = sorted(self.captures, key=lambda c: c['log_rect'].x())
        
        for cap in sorted_captures:
            # Check intersection in LOGICAL space
            intersect = selection_rect.intersected(cap['log_rect'])
            if not intersect.isEmpty():
                # Map intersection to Source Physical Coords
                rx = intersect.x() - cap['log_rect'].x()
                ry = intersect.y() - cap['log_rect'].y()
                rw = intersect.width()
                rh = intersect.height()
                
                # Scale factors
                # pixmap width is logical if DPR is set.
                pix_log_w = cap['pixmap'].width()
                pix_log_h = cap['pixmap'].height()
                
                sx = pix_log_w / cap['log_rect'].width()
                sy = pix_log_h / cap['log_rect'].height()
                
                # Pro výpočet zdrojového výřezu v raw pixelech musíme zohlednit DPR
                dpr = cap['pixmap'].devicePixelRatio()
                
                # src_x/y v logických souřadnicích pixmapy
                log_src_x = rx * sx
                log_src_y = ry * sy
                log_src_w = rw * sx
                log_src_h = rh * sy
                
                # Převod na fyzické pixely pro copy()
                # copy() bere argumenty v logických souřadnicích, pokud je DPR nastaven?
                # Ne, QPixmap.copy() bere souřadnice v pixelech (záleží na verzi Qt, ale obvykle device dependent pixels)
                # Ale pokud je DPR nastaven, chování se může lišit.
                # Jistota: Copy z raw kopie s DPR 1.0
                
                temp_pix = QPixmap(cap['pixmap'])
                temp_pix.setDevicePixelRatio(1.0)
                
                raw_src_x = int(log_src_x * dpr)
                raw_src_y = int(log_src_y * dpr)
                raw_src_w = int(log_src_w * dpr)
                raw_src_h = int(log_src_h * dpr)
                
                src_x = max(0, raw_src_x)
                src_y = max(0, raw_src_y)
                src_w = min(raw_src_w, temp_pix.width() - src_x)
                src_h = min(raw_src_h, temp_pix.height() - src_y)
                
                if src_w > 0 and src_h > 0:
                    fragment = temp_pix.copy(src_x, src_y, src_w, src_h)
                    final_fragments.append(fragment)
                    total_output_width += src_w
                    max_output_height = max(max_output_height, src_h)
        
        if not final_fragments:
            return None

        result = QPixmap(total_output_width, max_output_height)
        result.fill(Qt.transparent)
        result.setDevicePixelRatio(1.0)
        
        painter = QPainter(result)
        current_x = 0
        
        for frag in final_fragments:
             painter.drawPixmap(current_x, 0, frag)
             current_x += frag.width()
             
        painter.end()
        if emit_signal:
            self.capture_taken.emit(result)
        return result
        
    def show_fullscreen_custom(self):
        # Instead of classic showFullScreen(), which often takes only primary monitor,
        # manually set geometry across all monitors.
        self.setGeometry(self.total_rect)
        self.show()
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay shown with geometry: {self.geometry()}")
        self.raise_()
        self.activateWindow()
        self.setFocus()
        
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        
        # Draw each screen capture in its logical position
        for cap in self.captures:
            painter.drawPixmap(cap['log_rect'], cap['pixmap'])
        
        # Poloprůhledný černý overlay
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))
        
        if self.start_pos and self.end_pos:
            # Vybraná oblast (logické souřadnice okna)
            selection_rect = QRect(self.start_pos, self.end_pos).normalized()
            
            # Vykreslit "čistou" část obrázku do výběru
            # We must iterate again and clip painter to selection
            painter.setClipRect(selection_rect)
            for cap in self.captures:
                painter.drawPixmap(cap['log_rect'], cap['pixmap'])
            painter.setClipping(False)
            
            # Nakreslit rámeček kolem výběru
            settings = QSettings(ORG_NAME, APP_NAME)
            border_color = str(settings.value(SETTINGS_SNIP_BORDER_COLOR, DEFAULT_SNIP_BORDER_COLOR) or DEFAULT_SNIP_BORDER_COLOR)
            try:
                border_width = int(settings.value(SETTINGS_SNIP_BORDER_WIDTH, DEFAULT_SNIP_BORDER_WIDTH) or DEFAULT_SNIP_BORDER_WIDTH)
            except Exception:
                border_width = DEFAULT_SNIP_BORDER_WIDTH

            color = QColor(border_color)
            if not color.isValid():
                color = QColor(DEFAULT_SNIP_BORDER_COLOR)
            border_width = max(1, min(int(border_width), 10))

            pen = QPen(color, border_width)
            painter.setPen(pen)
            painter.drawRect(selection_rect)
    
    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
        event.accept()


class TranslationSnippingOverlay(SnippingOverlay):
    """Overlay: select area → OCR → translate → render translation inside selection.

    This is a pragmatic 'Google Lens-like' UX (block overlay). It does not do per-word
    alignment or in-image reflow.
    """

    def __init__(self, tray_app, parent=None, fullscreen_auto: bool = True):
        super().__init__(parent=parent)
        self.tray_app = tray_app
        self.settings = QSettings(ORG_NAME, APP_NAME)
        self._fullscreen_auto = bool(fullscreen_auto)

        if self._fullscreen_auto:
            try:
                self.setCursor(QCursor(Qt.ArrowCursor))
            except Exception:
                pass

        self._locked = False
        self._selection_rect = None
        self._captured_pixmap = None

        self._status = "Preparing…" if self._fullscreen_auto else "Select an area"
        self._status_detail = ""
        self._translated_text = ""

        self._lens_lines = []  # list[dict]: {"band": QRect, "text": str}

        # Simple loading indicator so users can tell it's working.
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.setTextVisible(True)
        self._progress.setFormat("Working…")
        self._progress.hide()

        self._ocr_service = getattr(self.tray_app, "ocr_service", None)

        self._ocr_engine_last = ""
        self._ocr_fallback_tried = False
        self._ocr_payload_last: dict | None = None

        self._tx_thread = None
        self._tx_worker = None

    def _summarize_status(self, message: str) -> str:
        s = str(message or "").replace("\r", "").strip()
        if not s:
            return ""
        # Hide noisy tracebacks in the one-line UI.
        if "Traceback:" in s:
            s = s.split("Traceback:", 1)[0].strip()
        # Collapse whitespace/newlines.
        s = " ".join(s.split())
        if len(s) > 220:
            s = s[:220].rstrip() + "…"
        return s

    def _set_status(self, status: str, detail: str = "") -> None:
        self._status = self._summarize_status(status) or "Working…"
        self._status_detail = str(detail or "")

    def _show_details_dialog(self, title: str, detail: str) -> None:
        try:
            dlg = QDialog(self)
            dlg.setWindowTitle(title)
            dlg.setModal(True)
            dlg.resize(900, 520)

            layout = QVBoxLayout(dlg)
            info = QLabel("Detaily (Ctrl+C kopíruje do schránky)")
            layout.addWidget(info)

            edit = QTextEdit()
            edit.setReadOnly(True)
            edit.setLineWrapMode(QTextEdit.NoWrap)
            edit.setPlainText(detail or "")
            layout.addWidget(edit)

            btns = QHBoxLayout()
            btn_copy = QPushButton("Copy")
            btn_close = QPushButton("Close")
            btns.addWidget(btn_copy)
            btns.addStretch(1)
            btns.addWidget(btn_close)
            layout.addLayout(btns)

            def _copy():
                try:
                    QApplication.clipboard().setText(edit.toPlainText() or "")
                except Exception:
                    pass

            btn_copy.clicked.connect(_copy)
            btn_close.clicked.connect(dlg.close)
            dlg.exec()
        except Exception:
            pass

    def mousePressEvent(self, event):  # noqa: N802
        if self._locked:
            event.accept()
            return
        if self._fullscreen_auto:
            # Ignore manual selection in fullscreen-auto mode.
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._locked:
            event.accept()
            return
        if self._fullscreen_auto:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._locked:
            event.accept()
            return

        if self._fullscreen_auto:
            event.accept()
            return

        if event.button() == Qt.LeftButton and self.is_selecting:
            self.is_selecting = False
            self.end_pos = event.position().toPoint()

            selection_rect = QRect(self.start_pos, self.end_pos).normalized()
            if selection_rect.width() > 10 and selection_rect.height() > 10:
                self._selection_rect = selection_rect
                self._captured_pixmap = self.process_capture(selection_rect, emit_signal=False)
                if self._captured_pixmap is None or self._captured_pixmap.isNull():
                    self.close()
                    return

                self._locked = True
                self._status = "Running OCR…"
                self.update()
                QTimer.singleShot(0, self._start_ocr)
            else:
                self.update()
        event.accept()

    def show_fullscreen_custom(self):
        super().show_fullscreen_custom()
        if self._fullscreen_auto:
            QTimer.singleShot(0, self._start_fullscreen_flow)

    def resizeEvent(self, event):  # noqa: N802
        try:
            # Keep the progress bar centered at the top.
            w = max(240, min(520, int(self.width() * 0.5)))
            self._progress.setGeometry(int((self.width() - w) / 2), 16, w, 18)
        except Exception:
            pass
        super().resizeEvent(event)

    def _set_busy(self, busy: bool, label: str = "Working…") -> None:
        try:
            self._progress.setFormat(label)
            self._progress.setVisible(bool(busy))
        except Exception:
            pass
        try:
            if self._fullscreen_auto:
                self.setCursor(QCursor(Qt.WaitCursor if busy else Qt.ArrowCursor))
        except Exception:
            pass

    def _start_fullscreen_flow(self) -> None:
        if self._locked:
            return

        # Hide selection UI; we translate the whole visible desktop.
        self.start_pos = None
        self.end_pos = None
        self.is_selecting = False

        full_rect = self.rect()
        if full_rect.isEmpty() or full_rect.width() < 10 or full_rect.height() < 10:
            self._status = "Capture error: invalid screen geometry."
            self.update()
            return

        self._selection_rect = full_rect
        self._captured_pixmap = self.process_capture(full_rect, emit_signal=False)
        if self._captured_pixmap is None or self._captured_pixmap.isNull():
            self._status = "Capture error: could not capture screen."
            self.update()
            return

        self._locked = True
        self._ocr_fallback_tried = False
        self._ocr_payload_last = None
        self._ocr_engine_last = ""
        self._set_status("Running OCR…")
        self._set_busy(True, "OCR…")
        self.update()
        QTimer.singleShot(0, self._start_ocr)

    def _compute_ocr_scale_for_pixmap(self, pixmap: QPixmap) -> int:
        use_auto_scale = bool(self.settings.value(SETTINGS_OCR_SCALE_AUTO, True, type=bool))
        try:
            scale = int(self.settings.value(SETTINGS_OCR_SCALE, 1) or 1)
        except Exception:
            scale = 1

        if use_auto_scale:
            try:
                w = int(pixmap.width())
                h = int(pixmap.height())
            except Exception:
                w, h = 0, 0
            area = w * h
            if area <= 0:
                scale = 2
            elif area < 700_000:
                scale = 3
            elif area < 2_200_000:
                scale = 2
            else:
                scale = 1

        return max(1, min(int(scale), 4))

    def _start_ocr(self) -> None:
        if self._ocr_service is None:
            self._set_status("OCR Error: OCR service is not available.")
            self._set_busy(False)
            self.update()
            return

        pix = self._captured_pixmap
        if pix is None or pix.isNull():
            self._set_status("OCR Error: Missing image.")
            self._set_busy(False)
            self.update()
            return

        use_greedy = bool(self.settings.value(SETTINGS_OCR_USE_GREEDY, True, type=bool))
        scale = self._compute_ocr_scale_for_pixmap(pix)

        # Translation overlay prefers fast Windows OCR when available.
        windows_ok = _windows_ocr_available()
        default_engine = "windows" if windows_ok else "paddle"
        engine_setting = str(self.settings.value(SETTINGS_OCR_ENGINE, default_engine) or default_engine).strip().lower()
        if engine_setting not in ("windows", "paddle"):
            engine_setting = default_engine
        engine = "windows" if windows_ok else engine_setting

        self._lens_lines = []
        self._translated_text = ""

        payload = {
            "engine": engine,
            "use_greedy": use_greedy,
            "image_path": None,
            "png_b64": None,
            "scale": scale,
        }

        # For Lens-like placement, we need bounding boxes.
        # - PaddleOCR: yes
        # - Windows OCR: now supports word boxes too
        if engine in ("paddle", "windows"):
            payload["return_boxes"] = True

        self._ocr_payload_last = dict(payload)
        self._ocr_engine_last = engine

        buf = QBuffer()
        buf.open(QBuffer.ReadWrite)
        ok = pix.save(buf, "PNG")
        if not ok:
            self._set_status("OCR Error: Could not encode image.")
            self._set_busy(False)
            self.update()
            return
        payload["png_b64"] = base64.b64encode(bytes(buf.data())).decode("ascii")
        buf.close()

        # Persist payload with encoded image for possible fallback retry.
        try:
            self._ocr_payload_last = dict(payload)
        except Exception:
            self._ocr_payload_last = None

        if payload.get("return_boxes"):
            self._ocr_service.submit(payload, self._on_ocr_finished_detail, self._on_ocr_error)
        else:
            self._ocr_service.submit(payload, self._on_ocr_finished, self._on_ocr_error)

    def _on_ocr_error(self, message: str) -> None:
        detail = str(message or "OCR failed")

        # If PaddleOCR timed out, auto-fallback to Windows OCR once (when available).
        msg_l = detail.lower()
        if (
            not self._ocr_fallback_tried
            and self._ocr_engine_last == "paddle"
            and ("timeout" in msg_l or "stuck" in msg_l or "extremely slow" in msg_l)
            and _windows_ocr_available()
        ):
            self._ocr_fallback_tried = True
            payload = dict(self._ocr_payload_last or {})
            payload["engine"] = "windows"
            payload.pop("return_boxes", None)
            self._ocr_engine_last = "windows"
            self._set_status("PaddleOCR is slow here → retrying with Windows OCR…")
            self._set_busy(True, "OCR (Windows)…")
            self.update()
            try:
                self._ocr_service.submit(payload, self._on_ocr_finished, self._on_ocr_error)
                return
            except Exception:
                # Fall through to showing the original error.
                pass

        short = self._summarize_status(detail) or "OCR failed"
        self._set_status(f"{short} (F1 for details)", detail=detail)
        try:
            print(detail)
        except Exception:
            pass
        self._set_busy(False)
        self.update()

    def _on_ocr_finished(self, out_text: str) -> None:
        text = (out_text or "").strip()
        if not text:
            self._set_status("OCR finished: no text detected.")
            self._set_busy(False)
            self.update()
            return

        source_lang = str(
            self.settings.value(SETTINGS_TRANSLATION_SOURCE_LANGUAGE, DEFAULT_TRANSLATION_SOURCE_LANGUAGE)
            or DEFAULT_TRANSLATION_SOURCE_LANGUAGE
        )
        target_lang = str(
            self.settings.value(SETTINGS_TRANSLATION_LANGUAGE, DEFAULT_TRANSLATION_LANGUAGE)
            or DEFAULT_TRANSLATION_LANGUAGE
        )

        engine = str(
            self.settings.value(SETTINGS_TRANSLATION_ENGINE, DEFAULT_TRANSLATION_ENGINE)
            or DEFAULT_TRANSLATION_ENGINE
        ).strip().lower()

        self._set_status("Translating (offline)…" if engine != "public" else "Translating (online)…")
        self._set_busy(True, "Translating…")
        self.update()

        try:
            self._tx_thread = QThread(self)
            self._tx_worker = _TranslateWorker(text=text, target_lang=target_lang, source_lang=source_lang)
            self._tx_worker.moveToThread(self._tx_thread)
            self._tx_thread.started.connect(self._tx_worker.run)
            self._tx_worker.finished.connect(self._on_translate_finished)
            self._tx_worker.error.connect(self._on_translate_error)
            self._tx_worker.finished.connect(lambda *_: self._cleanup_translate_thread())
            self._tx_worker.error.connect(lambda *_: self._cleanup_translate_thread())
            self._tx_thread.start()
        except Exception as e:
            detail = f"Translate error: {e}\n\nTraceback:\n{traceback.format_exc()}"
            self._set_status("Translate error (F1 for details)", detail=detail)
            self.update()

    def _on_ocr_finished_detail(self, resp: object) -> None:
        # Expecting: {ok:true, text:str, items:[{text,box,score}], orig_size:{w,h}, proc_size:{w,h}}
        if not isinstance(resp, dict):
            self._set_status("OCR Error: invalid response")
            self._set_busy(False)
            self.update()
            return

        raw_text = str(resp.get("text") or "").strip()
        items = resp.get("items") or []
        if not raw_text and not items:
            self._set_status("OCR finished: no text detected.")
            self._set_busy(False)
            self.update()
            return

        pix = self._captured_pixmap
        if pix is None or pix.isNull() or self._selection_rect is None:
            self._set_status("OCR Error: Missing image.")
            self._set_busy(False)
            self.update()
            return

        try:
            orig_w = int((resp.get("orig_size") or {}).get("w") or pix.width())
            orig_h = int((resp.get("orig_size") or {}).get("h") or pix.height())
        except Exception:
            orig_w, orig_h = pix.width(), pix.height()
        try:
            proc_w = int((resp.get("proc_size") or {}).get("w") or orig_w)
            proc_h = int((resp.get("proc_size") or {}).get("h") or orig_h)
        except Exception:
            proc_w, proc_h = orig_w, orig_h

        # Build token boxes in *original* pixmap space.
        token_boxes = []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = str(it.get("text") or "").strip()
            box = it.get("box")
            if not t or not isinstance(box, (list, tuple)):
                continue
            xs = []
            ys = []
            for p in box:
                if not isinstance(p, (list, tuple)) or len(p) != 2:
                    continue
                try:
                    xs.append(float(p[0]))
                    ys.append(float(p[1]))
                except Exception:
                    pass
            if not xs or not ys:
                continue

            # Map from processed OCR image space back to original pixmap space.
            sx = (orig_w / float(proc_w)) if proc_w else 1.0
            sy = (orig_h / float(proc_h)) if proc_h else 1.0
            x0 = min(xs) * sx
            x1 = max(xs) * sx
            y0 = min(ys) * sy
            y1 = max(ys) * sy
            token_boxes.append({"text": t, "x0": x0, "x1": x1, "y0": y0, "y1": y1})

        if not token_boxes:
            # Fallback to classic block overlay.
            self._on_ocr_finished(raw_text)
            return

        # Sort and group into lines.
        token_boxes.sort(key=lambda d: (d["y0"], d["x0"]))
        heights = [(tb["y1"] - tb["y0"]) for tb in token_boxes if (tb["y1"] - tb["y0"]) > 0]
        median_h = sorted(heights)[len(heights) // 2] if heights else 12.0
        y_thresh = max(6.0, 0.65 * float(median_h))

        lines = []
        current = None
        for tb in token_boxes:
            yc = 0.5 * (tb["y0"] + tb["y1"])
            if current is None:
                current = {"tokens": [tb], "yc": yc}
                continue
            if abs(yc - current["yc"]) <= y_thresh:
                current["tokens"].append(tb)
                # update running center
                current["yc"] = (current["yc"] * 0.7) + (yc * 0.3)
            else:
                lines.append(current)
                current = {"tokens": [tb], "yc": yc}
        if current is not None:
            lines.append(current)

        # Normalize tokens order inside line by x.
        for ln in lines:
            ln["tokens"].sort(key=lambda d: d["x0"])

        # Map to overlay selection rect space.
        sel = self._selection_rect
        try:
            mx = sel.width() / float(orig_w) if orig_w else 1.0
            my = sel.height() / float(orig_h) if orig_h else 1.0
        except Exception:
            mx, my = 1.0, 1.0

        overlay_lines = []
        for ln in lines:
            toks = ln["tokens"]
            text = " ".join([t["text"] for t in toks]).strip()
            if not text:
                continue
            x0 = min(t["x0"] for t in toks) * mx + sel.x()
            x1 = max(t["x1"] for t in toks) * mx + sel.x()
            y0 = min(t["y0"] for t in toks) * my + sel.y()
            y1 = max(t["y1"] for t in toks) * my + sel.y()
            yc = 0.5 * (y0 + y1)
            overlay_lines.append({"src": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1, "yc": yc})

        if not overlay_lines:
            self._on_ocr_finished(raw_text)
            return

        overlay_lines.sort(key=lambda d: d["yc"])

        # Build non-overlapping bands between lines (Lens-like).
        band_pad = 4
        inner_pad = 10
        inner = QRect(sel.x() + inner_pad, sel.y() + inner_pad, max(10, sel.width() - 2 * inner_pad), max(10, sel.height() - 2 * inner_pad))

        y_centers = [d["yc"] for d in overlay_lines]
        bands = []
        for i, d in enumerate(overlay_lines):
            if i == 0:
                top = max(float(inner.y()), float(d["y0"]) - 2 * band_pad)
            else:
                top = 0.5 * (y_centers[i - 1] + y_centers[i])
            if i == len(overlay_lines) - 1:
                bottom = min(float(inner.y() + inner.height()), float(d["y1"]) + 2 * band_pad)
            else:
                bottom = 0.5 * (y_centers[i] + y_centers[i + 1])

            top_i = int(max(inner.y(), top))
            bot_i = int(min(inner.y() + inner.height(), bottom))
            if bot_i - top_i < 10:
                continue
            bands.append({"band": QRect(inner.x(), top_i, inner.width(), bot_i - top_i), "src": d["src"]})

        if not bands:
            self._on_ocr_finished(raw_text)
            return

        self._lens_lines = [{"band": b["band"], "text": "", "src": b["src"]} for b in bands]

        source_lang = str(
            self.settings.value(SETTINGS_TRANSLATION_SOURCE_LANGUAGE, DEFAULT_TRANSLATION_SOURCE_LANGUAGE)
            or DEFAULT_TRANSLATION_SOURCE_LANGUAGE
        )
        target_lang = str(
            self.settings.value(SETTINGS_TRANSLATION_LANGUAGE, DEFAULT_TRANSLATION_LANGUAGE)
            or DEFAULT_TRANSLATION_LANGUAGE
        )
        engine = str(
            self.settings.value(SETTINGS_TRANSLATION_ENGINE, DEFAULT_TRANSLATION_ENGINE)
            or DEFAULT_TRANSLATION_ENGINE
        ).strip().lower()

        self._set_status("Translating (offline)…" if engine != "public" else "Translating (online)…")
        self._set_busy(True, "Translating…")
        self.update()

        lines_to_translate = [b["src"] for b in bands]

        class _TranslateLinesWorker(QObject):
            finished = Signal(object)
            error = Signal(str)

            def __init__(self, lines: list[str], target: str, source: str | None, parent=None):
                super().__init__(parent)
                self._lines = lines
                self._target = target
                self._source = source
                self._public = PublicAiClient()
                self._argos = LocalArgosTranslateClient()

            def run(self) -> None:
                try:
                    settings = QSettings(ORG_NAME, APP_NAME)
                    engine = str(settings.value(SETTINGS_TRANSLATION_ENGINE, DEFAULT_TRANSLATION_ENGINE) or DEFAULT_TRANSLATION_ENGINE).strip().lower()
                    auto_models = bool(settings.value(SETTINGS_TRANSLATION_AUTO_DOWNLOAD_MODELS, True, type=bool))
                    out = []
                    for s in self._lines:
                        if not (s or "").strip():
                            out.append("")
                            continue
                        if engine == "public":
                            out.append(self._public.translate_text(s, self._target))
                        else:
                            out.append(self._argos.translate_text(s, self._source or DEFAULT_TRANSLATION_SOURCE_LANGUAGE, self._target, auto_models))
                    self.finished.emit(out)
                except Exception as e:
                    self.error.emit(str(e))

        try:
            self._tx_thread = QThread(self)
            self._tx_worker = _TranslateLinesWorker(lines=lines_to_translate, target=target_lang, source=source_lang)
            self._tx_worker.moveToThread(self._tx_thread)
            self._tx_thread.started.connect(self._tx_worker.run)

            def _ok(translated_lines: object) -> None:
                if isinstance(translated_lines, list):
                    for i in range(min(len(self._lens_lines), len(translated_lines))):
                        self._lens_lines[i]["text"] = str(translated_lines[i] or "").strip()
                    self._set_status("Done (Esc to close)")
                    self._set_busy(False)
                else:
                    self._set_status("Translate error: invalid response")
                    self._set_busy(False)
                self.update()

            def _err(msg: str) -> None:
                detail = str(msg or "Translate failed")
                self._set_status("Translate error (F1 for details)", detail=detail)
                self._set_busy(False)
                self.update()

            self._tx_worker.finished.connect(_ok)
            self._tx_worker.error.connect(_err)
            self._tx_worker.finished.connect(lambda *_: self._cleanup_translate_thread())
            self._tx_worker.error.connect(lambda *_: self._cleanup_translate_thread())
            self._tx_thread.start()
        except Exception as e:
            detail = f"Translate error: {e}\n\nTraceback:\n{traceback.format_exc()}"
            self._set_status("Translate error (F1 for details)", detail=detail)
            self.update()

    def _cleanup_translate_thread(self) -> None:
        try:
            if self._tx_thread is not None and self._tx_thread.isRunning():
                self._tx_thread.quit()
                self._tx_thread.wait(1500)
        except Exception:
            pass
        self._tx_thread = None
        self._tx_worker = None

    def _on_translate_error(self, message: str) -> None:
        detail = str(message or "Translate failed")
        self._set_status("Translate error (F1 for details)", detail=detail)
        self._set_busy(False)
        self.update()

    def _on_translate_finished(self, translated: str) -> None:
        self._translated_text = (translated or "").strip()
        self._set_status("Done (Esc to close)" if self._translated_text else "Translation returned empty text.")
        self._set_busy(False)
        self.update()

    def keyPressEvent(self, event):  # noqa: N802
        try:
            if event.key() == Qt.Key_Escape:
                self.close()
                event.accept()
                return

            if event.key() == Qt.Key_F1:
                detail = (self._status_detail or "").strip()
                if detail:
                    self._show_details_dialog("Details", detail)
                event.accept()
                return

            if event.matches(QKeySequence.Copy):
                # Prefer copying error details if present; otherwise copy translation.
                text = (self._status_detail or "").strip()
                if not text:
                    if self._lens_lines:
                        text = "\n".join([str(ln.get("text") or "").strip() for ln in self._lens_lines if str(ln.get("text") or "").strip()])
                    else:
                        text = (self._translated_text or "").strip()
                if text:
                    QApplication.clipboard().setText(text)
                event.accept()
                return
        except Exception:
            pass

        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.close()
        event.accept()

    def _fit_font_to_rect(self, painter: QPainter, rect: QRect, text: str, start_pt: int) -> QFont:
        pt = max(8, int(start_pt))
        while pt > 8:
            font = QFont("Segoe UI", pt)
            painter.setFont(font)
            fm = QFontMetrics(font)
            br = fm.boundingRect(rect, Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, text)
            if br.height() <= rect.height() and br.width() <= rect.width():
                return font
            pt -= 1
        return QFont("Segoe UI", 8)

    def paintEvent(self, event):  # noqa: N802
        # In fullscreen-auto mode we want a Lens-like overlay: keep the screen visible (no global dimming
        # and no selection visuals), then draw translated bands on top.
        if self._fullscreen_auto:
            painter = QPainter(self)
            # Draw each screen capture in its logical position.
            for cap in self.captures:
                try:
                    painter.drawPixmap(cap["log_rect"], cap["pixmap"])
                except Exception:
                    pass
        else:
            super().paintEvent(event)
            painter = QPainter(self)

        # Status line (always)
        try:
            status_rect = QRect(10, 8, self.width() - 20, 26)
            painter.fillRect(status_rect, QColor(0, 0, 0, 140))
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont("Segoe UI", 11))
            fm = QFontMetrics(painter.font())
            text = fm.elidedText(str(self._status or ""), Qt.ElideRight, max(10, status_rect.width() - 16))
            painter.drawText(status_rect.adjusted(8, 0, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, text)
        except Exception:
            pass

        if not self._selection_rect:
            return

        # Lens-like mode: draw per-line translated bands when available.
        if self._lens_lines:
            rect = self._selection_rect
            for ln in self._lens_lines:
                band = ln.get("band")
                text = str(ln.get("text") or "").strip()
                if not isinstance(band, QRect) or not text:
                    continue
                try:
                    painter.fillRect(band, QColor(255, 255, 255, 220))
                except Exception:
                    pass
                try:
                    start_pt = max(9, min(26, int(band.height() / 2.2)))
                    font = self._fit_font_to_rect(painter, band.adjusted(6, 2, -6, -2), text, start_pt=start_pt)
                    painter.setFont(font)
                    painter.setPen(QColor(0, 0, 0))
                    painter.drawText(band.adjusted(6, 2, -6, -2), Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignVCenter, text)
                except Exception:
                    pass
            return

        # Fallback: classic block overlay.
        if not self._translated_text:
            return

        rect = self._selection_rect
        pad = 10
        inner = QRect(
            rect.x() + pad,
            rect.y() + pad,
            max(10, rect.width() - 2 * pad),
            max(10, rect.height() - 2 * pad),
        )

        # Darken selection and overlay translated text
        try:
            painter.fillRect(rect, QColor(0, 0, 0, 150))
        except Exception:
            pass

        try:
            start_pt = max(10, min(34, int(rect.height() / 16)))
            font = self._fit_font_to_rect(painter, inner, self._translated_text, start_pt=start_pt)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(inner, Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, self._translated_text)
        except Exception:
            pass


class TrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # Single instance check
        self.shared_memory = QSharedMemory("TxtOnScrn_SingleInstance")
        if not self.shared_memory.create(1):
            # Instance already running, notify it to show config
            self.notify_existing_instance()
            sys.exit(0)

        # Setup local server to receive commands
        self.local_server = QLocalServer()
        QLocalServer.removeServer("TxtOnScrn_LocalServer")
        self.local_server.listen("TxtOnScrn_LocalServer")
        self.local_server.newConnection.connect(self.on_new_connection)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.current_hotkey = None
        self.editor_window = None
        self.ocr_assistant_flow = None

        # Shared OCR service (single long-lived OCR subprocess for the whole app)
        self.ocr_service = OcrService(parent=self.app)

        # Apply appearance theme early
        apply_theme_mode(self.settings.value(SETTINGS_THEME_MODE, "system"))

        # Signal for communication between keyboard thread and GUI thread
        self.signal_handler = HotkeySignal()
        self.signal_handler.hotkey_pressed.connect(self.on_hotkey_main_thread)
        self.signal_handler.translate_pressed.connect(self.on_translate_hotkey_main_thread)

        icon_path = resource_path("ico.ico")
        self.tray_icon = QSystemTrayIcon(QIcon(icon_path))

        self.settings_window = SettingsDialog(settings=self.settings, tray_app=self)

        menu = QMenu()
        config_action = menu.addAction("Config")
        quit_action = menu.addAction("Quit")
        config_action.triggered.connect(lambda: self.show_settings("general"))
        quit_action.triggered.connect(self.quit)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.show()

        self.register_hotkeys()
        
        self.snipping_overlay = None
        self.translation_overlay = None

    def notify_existing_instance(self):
        """Notify existing instance to show config window"""
        socket = QLocalSocket()
        socket.connectToServer("TxtOnScrn_LocalServer")
        if socket.waitForConnected(1000):
            socket.write(b"show_config")
            socket.flush()
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()

    def on_new_connection(self):
        """Handle connection from new instance"""
        client = self.local_server.nextPendingConnection()
        if client:
            client.waitForReadyRead(1000)
            data = client.readAll().data()
            if data == b"show_config":
                self.show_settings("general")
            client.disconnectFromServer()

    def register_hotkeys(self):
        # Main hotkey
        if self.current_hotkey:
            try:
                keyboard.remove_hotkey(self.current_hotkey)
            except Exception:
                pass

        hotkey = self.settings.value(SETTINGS_HOTKEY, DEFAULT_HOTKEY)
        self.current_hotkey = str(hotkey or DEFAULT_HOTKEY).lower().replace("ctrl", "control")

        try:
            keyboard.add_hotkey(self.current_hotkey, self.on_hotkey_pressed)
        except Exception:
            pass

        # Translation hotkey
        if getattr(self, "translation_hotkey", None):
            try:
                keyboard.remove_hotkey(self.translation_hotkey)
            except Exception:
                pass

        t_hotkey = self.settings.value(SETTINGS_TRANSLATION_HOTKEY, DEFAULT_TRANSLATION_HOTKEY)
        self.translation_hotkey = str(t_hotkey or DEFAULT_TRANSLATION_HOTKEY).lower().replace("ctrl", "control")

        try:
            keyboard.add_hotkey(self.translation_hotkey, self.on_translate_hotkey_pressed)
        except Exception:
            pass

    # Backward compatibility (older code paths)
    def register_hotkey(self):
        self.register_hotkeys()

    def on_hotkey_pressed(self):
        # Keyboard library callback runs in another thread
        # Must emit signal for GUI thread
        self.signal_handler.hotkey_pressed.emit()

    def on_translate_hotkey_pressed(self):
        self.signal_handler.translate_pressed.emit()

    def on_hotkey_main_thread(self):
        # Toggle overlay: if running, close; else open
        
        # Check existing instance
        if self.snipping_overlay:
            try:
                # Zkusíme zjistit, zda je okno viditelné
                # Pokud bylo C++ objektem smazáno, vyhodí toto RuntimeError
                if self.snipping_overlay.isVisible():
                    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay closed")
                    self.snipping_overlay.close()
                    return
            except RuntimeError:
                # Objekt byl smazán, ale reference zůstala. Vyčistíme.
                self.snipping_overlay = None

        # Create and show new overlay for selection
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay enabled")
        self.snipping_overlay = SnippingOverlay()
        # Clear reference after closing
        try:
            self.snipping_overlay.destroyed.connect(self._overlay_destroyed)
            self.snipping_overlay.capture_taken.connect(self.open_editor)
        except Exception:
            pass
        self.snipping_overlay.show_fullscreen_custom()
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay shown")

    def on_translate_hotkey_main_thread(self):
        # Toggle translation overlay
        if self.translation_overlay:
            try:
                if self.translation_overlay.isVisible():
                    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Translation overlay closed")
                    self.translation_overlay.close()
                    return
            except RuntimeError:
                self.translation_overlay = None

        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Translation overlay enabled")
        self.translation_overlay = TranslationSnippingOverlay(tray_app=self)
        try:
            self.translation_overlay.destroyed.connect(lambda *_: setattr(self, "translation_overlay", None))
        except Exception:
            pass
        self.translation_overlay.show_fullscreen_custom()
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Translation overlay shown")

    def open_editor(self, pixmap: QPixmap):
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Opening editor")

        # Optional: open Assistant directly (skip editor) and feed OCR text as its input context
        try:
            raw_flag = self.settings.value(SETTINGS_OCR_ASSISTANT_AFTER_CAPTURE, False)
            if isinstance(raw_flag, bool):
                flag = raw_flag
            else:
                s = str(raw_flag).strip().lower()
                flag = s in {"1", "true", "yes", "y", "on"}
            print(
                f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] assistant_after_capture={raw_flag!r} ({type(raw_flag).__name__}) -> {flag}"
            )

            if flag:
                if self.ocr_assistant_flow is not None:
                    try:
                        self.ocr_assistant_flow.shutdown()
                    except Exception:
                        pass
                self.ocr_assistant_flow = OcrAssistantFlow(tray_app=self, pixmap=pixmap)
                self.ocr_assistant_flow.show()
                return
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] assistant_after_capture failed: {e}\n{traceback.format_exc()}")

        # If editor already exists, close or update it
        if self.editor_window:
            try:
                self.editor_window.close()
            finally:
                # Ensure we don't keep a stale reference if close does not immediately delete.
                self.editor_window = None

        self.editor_window = EditorWindow(pixmap, tray_app=self)
        self.editor_window.show()

        # Optional: run OCR automatically right after capture
        try:
            if bool(self.settings.value(SETTINGS_OCR_AUTO_RUN, False, type=bool)):
                QTimer.singleShot(0, self.editor_window.run_ocr)
        except Exception:
            pass

    def _overlay_destroyed(self, obj=None):
        print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] Overlay destroyed")
        if self.snipping_overlay is obj:
            self.snipping_overlay = None
        elif self.snipping_overlay and obj is None:
             # Fallback pokud obj není předán, ale víme že se ničí
             pass
    
    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # Levý klik
            self.show_settings("general")

    def show_settings(self, tab_name: str = "general"):
        self.settings_window.refresh_ui()
        self.settings_window.show_tab(tab_name)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def focus_editor(self) -> bool:
        if not self.editor_window:
            return False
        try:
            self.editor_window.show()
            self.editor_window.raise_()
            self.editor_window.activateWindow()
            return True
        except Exception:
            return False

    def quit(self):
        if getattr(self, "translation_overlay", None):
            try:
                self.translation_overlay.close()
            except Exception:
                pass
            self.translation_overlay = None
        if self.ocr_assistant_flow is not None:
            try:
                self.ocr_assistant_flow.shutdown()
            except Exception:
                pass
            self.ocr_assistant_flow = None
        if self.editor_window:
            try:
                self.editor_window.close()
            except Exception:
                pass
            self.editor_window = None

        # Stop shared OCR subprocess
        try:
            if getattr(self, "ocr_service", None) is not None:
                self.ocr_service.shutdown()
        except Exception:
            pass
        self.ocr_service = None

        if getattr(self, "translation_hotkey", None):
            try:
                keyboard.remove_hotkey(self.translation_hotkey)
            except Exception:
                pass

        if self.current_hotkey:
            try:
                keyboard.remove_hotkey(self.current_hotkey)
            except:
                pass
        if hasattr(self, 'local_server'):
            self.local_server.close()
        if hasattr(self, 'shared_memory'):
            self.shared_memory.detach()
        self.tray_icon.hide()
        try:
            self.app.quit()
        except Exception:
            QApplication.quit()

    def run(self):
        sys.exit(self.app.exec())


def main():
    app = TrayApp()
    return app.run()


if __name__ == "__main__":
    main()
