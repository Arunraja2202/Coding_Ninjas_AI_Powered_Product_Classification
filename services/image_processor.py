from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Tuple, Any
import base64, hashlib, json, os, re, tempfile, time

from PIL import Image, ImageOps, ImageEnhance, ImageFilter

try:
    import cv2
except Exception:
    cv2 = None
try:
    import numpy as np
except Exception:
    np = None
try:
    import pytesseract
except Exception:
    pytesseract = None

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
_OCR_CACHE: Dict[str, dict] = {}
_PADDLE = None
_EASYOCR = None


def normalize_text(text: str) -> str:
    text = text or ""
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_key(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in {"nan", "none", "null"}:
        return ""
    s = re.sub(r"\.0$", "", s)
    return s


def key_variants(value) -> List[str]:
    s = normalize_key(value)
    if not s:
        return []
    vals = {s.lower()}
    if s.isdigit():
        vals.add(s.lstrip("0") or "0")
        vals.add(s.zfill(12))
        vals.add(s.zfill(13))
        # UPC-A may arrive without the leading zero in some source systems.
        if len(s) == 11:
            vals.add("0" + s)
    return list(vals)


def discover_images(folder: str | Path) -> List[Path]:
    folder = Path(folder)
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def image_key_candidates(path: Path) -> set[str]:
    keys = set()
    # The most important association is the UPC folder. Also inspect the filename.
    for part in [path.name, path.parent.name, *path.parts[-5:]]:
        stem = Path(part).stem
        if stem:
            keys.add(stem.lower())
            for token in re.findall(r"\d{6,14}", stem):
                keys.update(key_variants(token))
    return keys


def build_image_index(folder: str | Path) -> Dict[str, List[str]]:
    index: Dict[str, List[str]] = {}
    for p in discover_images(folder):
        for key in image_key_candidates(p):
            index.setdefault(key, []).append(str(p))
    for k, paths in index.items():
        index[k] = list(dict.fromkeys(paths))
    return index


def find_images_for_row(row, index: Dict[str, List[str]]) -> List[str]:
    candidate_keys = []
    for col in (
        "BARCODE", "UPC", "NAN_KEY", "NANKEY", "NAN KEY", "NAN KEY ",
        "ITEM_DESCRIPTION", "ITEM DESCRIPTION", "PRO ITEM DESCRIPTION ", "PRO ITEM DESCRIPTION"
    ):
        if col in row:
            candidate_keys.extend(key_variants(row.get(col)))
    paths: List[str] = []
    for key in candidate_keys:
        paths.extend(index.get(key, []))
    numeric = [k for k in candidate_keys if k.isdigit() and len(k) >= 6]
    if not paths and numeric:
        for key, values in index.items():
            if any(n in key for n in numeric):
                paths.extend(values)
    return list(dict.fromkeys(paths))


def _pil_to_cv(pil_img: Image.Image):
    if cv2 is None or np is None:
        return None
    rgb = np.array(pil_img.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _deskew(gray):
    if cv2 is None or np is None:
        return gray, 0.0
    try:
        # Estimate skew from foreground pixels. Avoid cv2.imread entirely so
        # Windows paths and unusual image codecs cannot break preprocessing.
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        coords = np.column_stack(np.where(thresh > 0))
        if len(coords) < 100:
            return gray, 0.0
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        if abs(angle) < 0.20 or abs(angle) > 18:
            return gray, 0.0
        h, w = gray.shape[:2]
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        out = cv2.warpAffine(gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        return out, float(angle)
    except Exception:
        return gray, 0.0


def preprocess_opencv(path: str | Path) -> Tuple[List[Tuple[str, Any]], dict]:
    """Generate multiple high-resolution OCR variants.

    PIL is used to decode the source image first. This deliberately avoids
    cv2.imread(), which can fail or be shadowed on Windows installations.
    """
    meta = {"opencv": bool(cv2 and np), "deskew_angle": 0.0, "source_size": None, "variants": []}
    try:
        pil = Image.open(path).convert("RGB")
        meta["source_size"] = list(pil.size)
    except Exception as e:
        meta["error"] = f"PIL open failed: {type(e).__name__}: {e}"
        return [], meta
    if cv2 is None or np is None:
        return [], meta
    img = _pil_to_cv(pil)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    # Packaging text is often small. Bring the short edge into a readable range.
    scale = max(1.0, min(3.0, 1800.0 / max(1, min(h, w))))
    if scale > 1.02:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    deskewed, angle = _deskew(enhanced)
    meta["deskew_angle"] = angle
    denoise = cv2.fastNlMeansDenoising(deskewed, None, 7, 7, 21)
    sharpen = cv2.addWeighted(denoise, 1.7, cv2.GaussianBlur(denoise, (0, 0), 1.2), -0.7, 0)
    adaptive = cv2.adaptiveThreshold(denoise, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 11)
    otsu = cv2.threshold(denoise, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    variants = [
        ("gray_clahe_deskew", deskewed),
        ("denoise_sharpen", sharpen),
        ("adaptive_threshold", adaptive),
        ("otsu_threshold", otsu),
    ]
    meta["variants"] = [x[0] for x in variants]
    return variants, meta


def _tesseract_on_image(image, psm_values=(6, 11)) -> str:
    if pytesseract is None:
        return ""
    chunks = []
    for psm in psm_values:
        try:
            txt = pytesseract.image_to_string(image, config=f"--oem 3 --psm {psm}", lang=os.getenv("OCR_LANG", "eng"))
            if txt and normalize_text(txt):
                chunks.append(txt)
        except Exception:
            continue
    return normalize_text("\n".join(chunks))


def _paddle_ocr(path: str | Path) -> dict:
    global _PADDLE
    if os.getenv("ENABLE_PADDLE_OCR", "true").lower() not in {"1", "true", "yes"}:
        return {"available": False, "text": "", "boxes": [], "error": "disabled"}
    try:
        from paddleocr import PaddleOCR
        if _PADDLE is None:
            # PaddleOCR performs detection + recognition. It is the preferred
            # detector/recognizer when installed; the UI exposes it separately.
            _PADDLE = PaddleOCR(lang="en", use_doc_orientation_classify=False,
                                use_doc_unwarping=False, use_textline_orientation=True)
        result = _PADDLE.predict(str(path))
        texts, scores, boxes = [], [], []
        for item in result:
            data = item.json() if hasattr(item, "json") else item
            if isinstance(data, str):
                data = json.loads(data)
            res = data.get("res", data) if isinstance(data, dict) else {}
            texts.extend([str(x) for x in (res.get("rec_texts") or []) if str(x).strip()])
            scores.extend([float(x) for x in (res.get("rec_scores") or [])])
            boxes.extend(res.get("rec_boxes") or res.get("dt_polys") or [])
        return {"available": True, "text": normalize_text("\n".join(texts)), "scores": scores, "boxes": boxes}
    except Exception as e:
        return {"available": False, "text": "", "boxes": [], "error": f"{type(e).__name__}: {e}"}


def _easyocr(path: str | Path) -> dict:
    global _EASYOCR
    if os.getenv("ENABLE_EASYOCR", "true").lower() not in {"1", "true", "yes"}:
        return {"available": False, "text": "", "boxes": [], "error": "disabled"}
    try:
        import easyocr
        if _EASYOCR is None:
            _EASYOCR = easyocr.Reader(["en"], gpu=False, verbose=False)
        results = _EASYOCR.readtext(str(path), detail=1, paragraph=False)
        texts = [str(x[1]) for x in results if len(x) > 1 and str(x[1]).strip()]
        scores = [float(x[2]) for x in results if len(x) > 2]
        boxes = [x[0] for x in results]
        return {"available": True, "text": normalize_text("\n".join(texts)), "scores": scores, "boxes": boxes}
    except Exception as e:
        return {"available": False, "text": "", "boxes": [], "error": f"{type(e).__name__}: {e}"}


def _craft(path: str | Path) -> dict:
    if os.getenv("ENABLE_CRAFT", "true").lower() not in {"1", "true", "yes"}:
        return {"available": False, "regions": 0, "text": "", "error": "disabled"}
    try:
        from craft_text_detector import Craft
        craft = Craft(output_dir=None, crop_type="box", cuda=False)
        prediction = craft.detect_text(str(path))
        boxes = prediction.get("boxes", []) if isinstance(prediction, dict) else []
        text = ""
        # CRAFT is a detector; OCR the detected regions with Tesseract.
        try:
            image = Image.open(path).convert("RGB")
            crops = []
            for box in boxes:
                pts = np.array(box).reshape(-1, 2) if np is not None else None
                if pts is None or len(pts) == 0:
                    continue
                x1, y1 = pts.min(axis=0).astype(int); x2, y2 = pts.max(axis=0).astype(int)
                x1, y1 = max(0, x1), max(0, y1); x2, y2 = min(image.width, x2), min(image.height, y2)
                if x2 > x1 and y2 > y1:
                    crops.append(image.crop((x1, y1, x2, y2)).resize(((x2-x1)*2, (y2-y1)*2)))
            text = normalize_text("\n".join(_tesseract_on_image(c, (7, 8)) for c in crops))
        finally:
            try: craft.unload_craftnet_model(); craft.unload_refinenet_model()
            except Exception: pass
        return {"available": True, "regions": len(boxes), "text": text}
    except Exception as e:
        return {"available": False, "regions": 0, "text": "", "error": f"{type(e).__name__}: {e}"}


def _dbnetpp(path: str | Path) -> dict:
    """Optional real DBNet++ ONNX hook.

    If DBNETPP_MODEL_PATH points to a compatible DBNet++ ONNX model and
    DBNETPP_COMMAND is not set, this reports the model as configured. Actual
    model-specific post-processing varies by exported model, so the application
    never fabricates detection results. PaddleOCR/CRAFT provide working
    detection fallbacks when installed.
    """
    model = os.getenv("DBNETPP_MODEL_PATH", "").strip()
    if not model:
        return {"available": False, "configured": False, "regions": 0, "error": "DBNETPP_MODEL_PATH not configured"}
    if not Path(model).exists():
        return {"available": False, "configured": True, "regions": 0, "error": "DBNet++ model path does not exist"}
    return {"available": True, "configured": True, "regions": None, "error": "Model configured; use deployment-specific DBNet++ post-processing adapter"}


def _dedupe_texts(texts: List[str]) -> str:
    unique, seen = [], set()
    for t in texts:
        t = normalize_text(t)
        if len(t) < 2:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
        if key and key not in seen:
            seen.add(key); unique.append(t)
    return normalize_text("\n".join(unique))


def read_image(path: str | Path) -> dict:
    """Run every locally available OCR/detection layer for ONE image.

    Returns an auditable object used by both the pipeline and the Image Read Test UI.
    """
    path = str(path)
    try:
        st = os.stat(path)
        cache_key = f"{path}|{st.st_size}|{st.st_mtime_ns}|{os.getenv('OCR_LANG','eng')}"
    except OSError:
        cache_key = path
    if cache_key in _OCR_CACHE:
        return _OCR_CACHE[cache_key]

    started = time.time()
    result = {
        "path": path, "filename": Path(path).name, "status": "READ",
        "engines": {}, "variants": [], "combined_text": "", "elapsed_ms": 0,
    }
    try:
        variants, prep_meta = preprocess_opencv(path)
        result["preprocess"] = prep_meta
        result["variants"] = [name for name, _ in variants]
        tess_texts = []
        for name, arr in variants:
            txt = _tesseract_on_image(arr, (6, 11, 12))
            result["engines"][f"tesseract:{name}"] = {"available": bool(pytesseract), "text": txt}
            if txt: tess_texts.append(txt)
        # Original image, including PIL decode fallback.
        try:
            original = Image.open(path).convert("RGB")
            original = ImageOps.exif_transpose(original)
            if max(original.size) < 2400:
                scale = min(2.5, 2400 / max(original.size))
                original = original.resize((int(original.width*scale), int(original.height*scale)), Image.Resampling.LANCZOS)
            txt = _tesseract_on_image(original, (6, 11, 12))
            result["engines"]["tesseract:original"] = {"available": bool(pytesseract), "text": txt}
            if txt: tess_texts.append(txt)
        except Exception as e:
            result["engines"]["tesseract:original"] = {"available": False, "text": "", "error": str(e)}

        paddle = _paddle_ocr(path)
        result["engines"]["paddleocr"] = paddle
        if paddle.get("text"): tess_texts.append(paddle["text"])
        easy = _easyocr(path)
        result["engines"]["easyocr"] = easy
        if easy.get("text"): tess_texts.append(easy["text"])
        craft = _craft(path)
        result["engines"]["craft"] = craft
        if craft.get("text"): tess_texts.append(craft["text"])
        result["engines"]["dbnetpp"] = _dbnetpp(path)
        result["combined_text"] = _dedupe_texts(tess_texts)
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = f"{type(e).__name__}: {e}"
    result["elapsed_ms"] = round((time.time() - started) * 1000)
    _OCR_CACHE[cache_key] = result
    return result


def ocr_image(path: str | Path) -> str:
    return read_image(path).get("combined_text", "")


def image_to_data_url(path: str | Path) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    mime = "image/jpeg" if ext in {".jpg", ".jpeg"} else "image/webp" if ext == ".webp" else "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def preview_variants(path: str | Path) -> dict:
    """Return small data URLs for the original and key OpenCV variants."""
    out = {"original": image_to_data_url(path)}
    variants, _ = preprocess_opencv(path)
    for name, arr in variants[:4]:
        if cv2 is None or np is None:
            continue
        ok, buf = cv2.imencode('.jpg', arr, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if ok:
            out[name] = 'data:image/jpeg;base64,' + base64.b64encode(buf.tobytes()).decode('ascii')
    return out


def image_manifest(folder: str | Path) -> dict:
    files = discover_images(folder)
    return {"count": len(files), "extensions": sorted({p.suffix.lower() for p in files})}
