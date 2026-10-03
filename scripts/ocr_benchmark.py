"""
OCR benchmark: would reading scanned CVs with Tesseract work, and at what cost?

Measurement only — CareerAI does not OCR (a recorded product decision,
docs/benchmarks/CV-Extraction.md, "Why there is still no OCR, on purpose").
This gives that decision numbers.

Every PDF case of the extraction corpus (apps/resumes/benchmark/corpus.py) is
read four ways and scored against the same truth by the same scorer:

  text   the text PDF, through the production reader (the baseline);
  clean  rasterised at 200 dpi and re-saved as an image-only PDF, then OCR;
  low    rasterised at 110 dpi, JPEG quality 30 and pixel noise, then OCR;
  skew   200 dpi, rotated 3 degrees, then OCR.

OCR runs only where production would need it — the reader found no text (its
NO_TEXT warning) — so the "text" row also shows that OCR would never trigger on
a readable PDF. Extra probes: a mixed PDF (header as an image, body as text), a
Nepali scan (character accuracy against the source lines), a malformed PDF.

    python scripts/ocr_benchmark.py --out var/benchmark/ocr-benchmark.json

Needs Tesseract 5 (TESSERACT_CMD, default C:\\Program Files\\Tesseract-OCR\\tesseract.exe)
with eng and nep language data, and pypdfium2.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

import psutil  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from apps.resumes.benchmark import corpus, scoring  # noqa: E402
from apps.resumes.parsing import extract_text, parse  # noqa: E402
from apps.resumes.parsing.base import ExtractionFailed  # noqa: E402
from apps.resumes.parsing.extract import NO_TEXT_WARNING  # noqa: E402

TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
PDF = "application/pdf"
OCR_DPI = 300
#: Past this, a page counts as a failure (a noisy page ran 400+ CPU seconds).
PAGE_TIMEOUT_SECONDS = 120


def rasterise(pdf: bytes, dpi: int) -> list[Image.Image]:
    document = pdfium.PdfDocument(pdf)
    return [page.render(scale=dpi / 72).to_pil().convert("L") for page in document]


def as_image_pdf(images: list[Image.Image], dpi: int) -> bytes:
    buffer = io.BytesIO()
    first, *rest = [image.convert("RGB") for image in images]
    first.save(buffer, "PDF", save_all=True, append_images=rest, resolution=dpi)
    return buffer.getvalue()


def degrade(pdf: bytes, variant: str) -> bytes:
    if variant == "clean":
        return as_image_pdf(rasterise(pdf, 200), 200)
    if variant == "skew":
        pages = [image.rotate(3, expand=True, fillcolor=255) for image in rasterise(pdf, 200)]
        return as_image_pdf(pages, 200)
    if variant == "low":
        rng = random.Random(7)  # noqa: S311 - reproducible image noise, not security
        pages = []
        for image in rasterise(pdf, 110):
            pixels = image.load()
            for _ in range(image.width * image.height // 60):
                x, y = rng.randrange(image.width), rng.randrange(image.height)
                pixels[x, y] = rng.choice((0, 120, 255))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=30)
            pages.append(Image.open(io.BytesIO(buffer.getvalue())).convert("L"))
        return as_image_pdf(pages, 110)
    raise ValueError(variant)


def tesseract(image: Image.Image, lang: str) -> tuple[str | None, float, int]:
    """Text, seconds and the tesseract process's peak resident memory in bytes."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "page.png"
        image.save(path)
        started = time.perf_counter()
        # Output to a file, not a pipe: a noisy page produces enough text to
        # fill the pipe while nothing reads it, and tesseract then waits for ever.
        base = Path(folder) / "out"
        process = subprocess.Popen(  # noqa: S603 - fixed binary, temp file arguments
            [TESSERACT, str(path), str(base), "-l", lang, "--psm", "3"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        watcher = psutil.Process(process.pid)
        peak = 0
        while process.poll() is None:
            if time.perf_counter() - started > PAGE_TIMEOUT_SECONDS:
                # Measured: one noisy page ran 400+ CPU seconds without finishing.
                process.kill()
                process.wait()
                return None, time.perf_counter() - started, peak
            try:
                peak = max(peak, watcher.memory_info().rss)
            except psutil.Error:
                break
            time.sleep(0.02)
        output = base.with_suffix(".txt")
        text = output.read_text(encoding="utf-8", errors="replace") if output.exists() else ""
        return text, time.perf_counter() - started, peak


def read(pdf: bytes, lang: str = "eng") -> dict:
    """Production reading, with OCR only where production finds no text."""
    started = time.perf_counter()
    try:
        text = extract_text(pdf, mime=PDF)
    except ExtractionFailed as exc:
        return {
            "parsed": None,
            "ocr": False,
            "error": str(exc),
            "seconds": time.perf_counter() - started,
        }
    parsed = parse(text, mime=PDF)
    extract_s = time.perf_counter() - started
    if NO_TEXT_WARNING not in parsed.warnings:
        return {
            "parsed": parsed,
            "ocr": False,
            "seconds": extract_s,
            "ocr_seconds": 0.0,
            "peak_mb": 0,
        }
    pages = rasterise(pdf, OCR_DPI)
    texts, ocr_s, peak, timeouts = [], 0.0, 0, 0
    for page in pages:
        page_text, seconds, page_peak = tesseract(page, lang)
        if page_text is None:
            timeouts += 1
            page_text = ""
        texts.append(page_text)
        ocr_s += seconds
        peak = max(peak, page_peak)
    ocr_text = "\n".join(texts)
    parsed = parse(ocr_text, mime=PDF)
    return {
        "parsed": parsed,
        "ocr": True,
        "seconds": extract_s + ocr_s,
        "ocr_seconds": ocr_s,
        "peak_mb": round(peak / 2**20),
        "pages": len(pages),
        "text": ocr_text,
        "timeouts": timeouts,
    }


def corpus_rows() -> dict:
    cases = [case for case in corpus.CASES if "pdf" in case.formats and not case.truth.is_picture]
    by_variant: dict[str, list] = {"text": [], "clean": [], "low": [], "skew": []}
    rows = []
    for case in cases:
        pdf = corpus.render(case, "pdf")
        for variant in by_variant:
            source = pdf if variant == "text" else degrade(pdf, variant)
            result = read(source)
            scored = scoring.score_case(case, "pdf", result["parsed"]) if result["parsed"] else None
            by_variant[variant].append(scored)
            rows.append(
                {
                    "case": case.slug,
                    "layout": case.layout,
                    "variant": variant,
                    "ocr_used": result["ocr"],
                    "f1": round(scored.f1, 3) if scored else None,
                    "fabrications": (
                        sum(len(s.spurious) for s in scored.fields.values()) + len(scored.forbidden)
                    )
                    if scored
                    else None,
                    "seconds": round(result["seconds"], 2),
                    "ocr_seconds": round(result.get("ocr_seconds", 0), 2),
                    "peak_mb": result.get("peak_mb"),
                    "timeouts": result.get("timeouts", 0),
                }
            )
            print(
                case.slug,
                variant,
                rows[-1]["f1"],
                f"{rows[-1]['seconds']}s",
                "ocr" if result["ocr"] else "",
                flush=True,
            )
    summary = {}
    for variant, results in by_variant.items():
        done = [r for r in results if r is not None]
        corpus_summary = scoring.score_corpus(done)
        variant_rows = [r for r in rows if r["variant"] == variant]
        seconds = [r["seconds"] for r in variant_rows]
        summary[variant] = {
            "cases": len(results),
            "macro_f1": round(corpus_summary.macro_f1, 3),
            "micro_f1": round(corpus_summary.micro_f1, 3),
            "clean_cases": corpus_summary.clean_cases,
            "fabrications": corpus_summary.fabrications,
            "field_f1": {name: round(score.f1, 3) for name, score in corpus_summary.totals.items()},
            "ocr_triggered": sum(1 for r in variant_rows if r["ocr_used"]),
            "seconds_median": round(statistics.median(seconds), 2),
            "seconds_max": round(max(seconds), 2),
            "peak_mb_max": max((r["peak_mb"] or 0) for r in variant_rows),
            "page_timeouts": sum(r.get("timeouts") or 0 for r in variant_rows),
            "two_column_f1": [r["f1"] for r in variant_rows if r["layout"] != "linear"],
        }
    return {"rows": rows, "summary": summary}


def mixed_probe() -> dict:
    """Header (name and contact) drawn as an image, body as real text."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    header = Image.new("L", (1400, 160), 255)
    draw = ImageDraw.Draw(header)
    font = ImageFont.truetype("arial.ttf", 44)
    draw.text((20, 20), "Anjali Thapa", fill=0, font=font)
    draw.text(
        (20, 90),
        "anjali.thapa@example.com  +977 9812345678",
        fill=0,
        font=ImageFont.truetype("arial.ttf", 32),
    )
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.drawImage(ImageReader(header), 40, 740, width=500, height=57)
    y = 700
    for line in (
        "EXPERIENCE",
        "Accountant, Himalayan Bank - 2021 - Present",
        "- Reconciled 40 branch accounts every month in Excel.",
        "SKILLS",
        "Excel, Tally, VAT",
    ):
        pdf.drawString(40, y, line)
        y -= 18
    pdf.save()
    result = read(buffer.getvalue())
    parsed = result["parsed"]
    return {
        "ocr_used": result["ocr"],
        "name_found": parsed.name if parsed else None,
        "emails_found": list(parsed.emails) if parsed else [],
        "note": "text layer present, so OCR never triggers; the image header is lost",
    }


def nepali_probe() -> dict:
    lines = [
        "माया कार्की",
        "पोखरा, नेपाल",
        "अनुभव",
        "फेवा स्टेज, ब्याकएन्ड डेभलपर, २०२२ देखि हालसम्म",
        "सीप: पाइथन, जाङ्गो, पोस्टग्रेसक्यूएल",
    ]
    font = ImageFont.truetype(r"C:\Windows\Fonts\Nirmala.ttc", 40)
    page = Image.new("L", (1654, 900), 255)
    draw = ImageDraw.Draw(page)
    for index, line in enumerate(lines):
        draw.text((80, 80 + index * 110), line, fill=0, font=font)
    pdf = as_image_pdf([page], 200)
    out = {}
    for lang in ("eng", "nep", "nep+eng"):
        result = read(pdf, lang=lang)
        found = (result.get("text") or "").strip()
        ratio = difflib.SequenceMatcher(None, "\n".join(lines), found).ratio()
        out[lang] = {
            "char_similarity": round(ratio, 3),
            "seconds": round(result["seconds"], 2),
            "sample": found[:120],
        }
    return out


def malformed_probe() -> dict:
    good = corpus.render(corpus.CASES[0], "pdf")
    result = read(good[: len(good) // 3])
    return {
        "ocr_used": result["ocr"],
        "error": result.get("error", ""),
        "parsed": result["parsed"] is not None,
    }


def main() -> None:
    # The Nepali probe prints Devanagari; a Windows console is not UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="var/benchmark/ocr-benchmark.json")
    options = parser.parse_args()
    version = subprocess.run([TESSERACT, "--version"], capture_output=True, text=True, check=False)  # noqa: S603
    report = {"tesseract": (version.stdout or version.stderr).splitlines()[0]}
    report.update(corpus_rows())
    report["mixed"] = mixed_probe()
    report["nepali"] = nepali_probe()
    report["malformed"] = malformed_probe()
    out = ROOT / options.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(
        json.dumps(
            {k: report[k] for k in ("tesseract", "summary", "mixed", "nepali", "malformed")},
            indent=1,
            ensure_ascii=False,
        )
    )
    print("report:", out)


if __name__ == "__main__":
    main()
