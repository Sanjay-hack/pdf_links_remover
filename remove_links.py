"""
remove_links.py
===============
Remove every clickable link from a PDF while keeping all visible text.

Four passes:
  1. pypdf   — removes /Link annotations and URI/GoTo/JS actions structurally
  2. PyMuPDF — deletes any remaining link annotations + doc-level scrub
  3. PyMuPDF — breaks viewer-auto-detected URLs/emails in plain text
  4. OCR     — for image-based PDFs (scanned pages with no text layer):
               uses Tesseract to find URL text in images and paints it white

Usage
-----
    python remove_links.py input.pdf
    python remove_links.py input.pdf -o cleaned.pdf
    python remove_links.py folder/ -o out_folder/

Install
-------
    pip install pymupdf pypdf cryptography pillow
    # For image-based PDFs also install Tesseract:
    # https://github.com/tesseract-ocr/tesseract
    # Windows: winget install UB-Mannheim.TesseractOCR
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import fitz
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

if not HAS_FITZ and not HAS_PYPDF:
    sys.exit("ERROR: Install at least one backend:  pip install pymupdf pypdf")

# ── Auto-detect Tesseract and set TESSDATA_PREFIX ─────────────────────────────
import os as _os
_TESSDATA_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tessdata",
    r"C:\Program Files (x86)\Tesseract-OCR\tessdata",
    _os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tessdata"),
    r"/usr/share/tesseract-ocr/5/tessdata",   # Ubuntu 22+ / Render Docker
    r"/usr/share/tesseract-ocr/4.00/tessdata",
    r"/usr/share/tessdata",
    r"/opt/homebrew/share/tessdata",
]
if not _os.environ.get("TESSDATA_PREFIX"):
    for _c in _TESSDATA_CANDIDATES:
        if _os.path.isfile(_os.path.join(_c, "eng.traineddata")):
            _os.environ["TESSDATA_PREFIX"] = _c
            break

# ── Regex patterns ────────────────────────────────────────────────────────────
_URL_RE = re.compile(
    r"(?:https?://|ftp://|www\.)"
    r"(?:[^\s<>\"'()\[\]{},]|(?<=[^\s])[,](?=[^\s]))*",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(
    r"(?:mailto:)?"
    r"[a-zA-Z0-9][a-zA-Z0-9._%+\-]*[a-zA-Z0-9]?"
    r"@"
    r"[a-zA-Z0-9][a-zA-Z0-9.\-]*[a-zA-Z0-9]"
    r"\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)
_TRAIL = str.maketrans("", "", ".,;:!?\"'()")


# ══════════════════════════════════════════════════════════════════════════════
# Pass 1 — pypdf structural cleanup
# ══════════════════════════════════════════════════════════════════════════════

def _resolve(obj):
    while isinstance(obj, IndirectObject):
        obj = obj.get_object()
    return obj


def _is_link_action(action) -> bool:
    action = _resolve(action)
    if not isinstance(action, DictionaryObject):
        return False
    if action.get("/S") in ("/URI", "/GoToR", "/Launch", "/JavaScript", "/Named"):
        return True
    nxt = action.get("/Next")
    if nxt is None:
        return False
    nxt = _resolve(nxt)
    if isinstance(nxt, DictionaryObject):
        return _is_link_action(nxt)
    if isinstance(nxt, ArrayObject):
        return any(_is_link_action(a) for a in nxt)
    return False


def _strip_page(page) -> int:
    if "/Annots" not in page:
        return 0
    annots = _resolve(page["/Annots"])
    if not isinstance(annots, ArrayObject):
        return 0
    removed = 0
    keep = ArrayObject()
    for ref in annots:
        annot = _resolve(ref)
        if not isinstance(annot, DictionaryObject):
            keep.append(ref)
            continue
        if annot.get("/Subtype") == "/Link":
            removed += 1
            continue
        for key in ("/A", "/AA"):
            if key in annot and _is_link_action(annot[key]):
                del annot[key]
                removed += 1
        keep.append(ref)
    if not keep:
        del page["/Annots"]
    else:
        page[NameObject("/Annots")] = keep
    return removed


def _strip_outlines(node, seen: set) -> int:
    node = _resolve(node)
    if not isinstance(node, DictionaryObject) or id(node) in seen:
        return 0
    seen.add(id(node))
    removed = 0
    for key in ("/A", "/AA"):
        if key in node:
            del node[key]
            removed += 1
    for k in ("/First", "/Next"):
        if k in node:
            removed += _strip_outlines(node[k], seen)
    return removed


def _strip_catalog(root: DictionaryObject) -> int:
    removed = 0
    for key in ("/OpenAction", "/AA"):
        if key in root:
            act = _resolve(root[key])
            if isinstance(act, DictionaryObject) and _is_link_action(act):
                del root[key]
                removed += 1
    if "/Names" in root:
        names = _resolve(root["/Names"])
        if isinstance(names, DictionaryObject) and "/JavaScript" in names:
            del names["/JavaScript"]
            removed += 1
    return removed


def clean_with_pypdf(src: str, dst: str, password: str = "") -> int:
    reader = PdfReader(src)
    if reader.is_encrypted:
        if reader.decrypt(password) == 0:
            raise PermissionError("Wrong password.")
    writer = PdfWriter(clone_from=reader)
    total  = sum(_strip_page(p) for p in writer.pages)
    root   = writer._root_object
    total += _strip_catalog(root)
    if "/Outlines" in root:
        total += _strip_outlines(root["/Outlines"], set())
    with open(dst, "wb") as fh:
        writer.write(fh)
    return total


# ══════════════════════════════════════════════════════════════════════════════
# Pass 2 — PyMuPDF annotation cleanup
# ══════════════════════════════════════════════════════════════════════════════

def clean_with_fitz(path: str) -> int:
    doc = fitz.open(path)
    removed = 0
    tmp_path = None
    try:
        for page in doc:
            links = [a for a in page.annots() if a.type[0] == fitz.PDF_ANNOT_LINK]
            for a in links:
                page.delete_annot(a)
                removed += 1
            for lk in page.get_links():
                try:
                    page.delete_link(lk)
                    removed += 1
                except Exception:
                    pass
        try:
            doc.scrub(remove_links=True)
        except Exception:
            pass
        with tempfile.NamedTemporaryFile(
                delete=False, suffix=".pdf", dir=str(Path(path).parent)) as tf:
            tmp_path = tf.name
        doc.save(tmp_path, incremental=False, deflate=True, garbage=4, clean=True)
    except Exception:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        doc.close()
        raise
    doc.close()
    shutil.move(tmp_path, path)
    return removed


# ══════════════════════════════════════════════════════════════════════════════
# Pass 3 — break viewer-auto-detected URLs/emails in plain text
# ══════════════════════════════════════════════════════════════════════════════

def _broken(text: str) -> str:
    """Insert U+200B (zero-width space) to break viewer URL/email detection."""
    ZW = "​"
    clean = text.lstrip("mailto:")
    if "@" in clean:
        return clean.replace("@", ZW + "@", 1)
    if "://" in text:
        return text.replace("://", ZW + "://", 1)
    if text.lower().startswith("www."):
        return "www" + ZW + text[3:]
    return text[:4] + ZW + text[4:]


def _span_props(page, rect):
    try:
        clip = rect + fitz.Rect(-1, -1, 1, 1)
        for blk in page.get_text("dict", clip=clip).get("blocks", []):
            for ln in blk.get("lines", []):
                for sp in ln.get("spans", []):
                    c = sp.get("color", 0)
                    return sp.get("size", 10.0), (
                        (c >> 16 & 0xFF) / 255.0,
                        (c >>  8 & 0xFF) / 255.0,
                        (c       & 0xFF) / 255.0,
                    )
    except Exception:
        pass
    return 10.0, (0.0, 0.0, 0.0)


# Matches any line that is a URL or contains a recognisable URL/domain/email.
_LINE_URL_RE = re.compile(
    r"https?://|ftp://"                              # explicit protocol
    r"|www\.[a-zA-Z0-9]"                             # www. prefix
    r"|(?:[a-zA-Z0-9-]+\.)+(?:com|org|eu|net|gov|edu|io|html|htm|pdf)(?:/|$|\s)"
    r"|[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",  # email
    re.IGNORECASE,
)


def break_autodetected_links(path: str) -> int:
    """
    Pass 3: find every line whose text contains a URL or domain name and
    blank-redact it.

    Why blank (no replacement text):
      - Replacement text with zero-width-space renders as a visible '?' in
        many PDF fonts, making the fix visible.
      - A blank redaction removes the characters from the content stream
        entirely — the safest, most viewer-agnostic approach.

    Why line-level (not search_for):
      - This PDF encodes ':' and '.' in URL text as separate glyph runs,
        so search_for("https://") may find 0 results even when the text
        is visually there.
      - get_text("dict") joins spans per line and DOES expose proper
        domain names with dots, so the regex reliably matches them.

    Fallback: for URLs that DO live in a single glyph run, we also try
    search_for("https://"), search_for("www."), search_for("@").
    """
    doc = fitz.open(path)
    total = 0
    tmp_path = None
    try:
        for page in doc:
            page_hits = 0
            covered: list[fitz.Rect] = []

            # ── Strategy A: line-level detection via get_text("dict") ─────────
            # Each 'line' dict has a 'bbox' and a list of 'spans'.
            # Joining span texts gives the full line string including dots/colons
            # that word-level extraction loses due to mixed font encoding.
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:      # skip image blocks
                    continue
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    if not spans:
                        continue
                    line_text = "".join(s.get("text", "") for s in spans)
                    if not _LINE_URL_RE.search(line_text):
                        continue

                    rect = fitz.Rect(line["bbox"])
                    if any(rect.intersects(c) for c in covered):
                        continue
                    try:
                        # Blank redaction — removes text from content stream
                        page.add_redact_annot(rect, fill=(1.0, 1.0, 1.0))
                        covered.append(rect)
                        page_hits += 1
                    except Exception:
                        pass

            # ── Strategy B: anchor search for URLs in single glyph runs ───────
            for anchor in ["https://", "http://", "ftp://", "www.", "@"]:
                for rect in page.search_for(anchor):
                    if any(rect.intersects(c) for c in covered):
                        continue
                    try:
                        page.add_redact_annot(rect, fill=(1.0, 1.0, 1.0))
                        covered.append(rect)
                        page_hits += 1
                    except Exception:
                        pass

            if page_hits:
                try:
                    page.apply_redactions()
                    total += page_hits
                except Exception:
                    pass

        if total:
            with tempfile.NamedTemporaryFile(
                    delete=False, suffix=".pdf", dir=str(Path(path).parent)) as tf:
                tmp_path = tf.name
            doc.save(tmp_path, incremental=False, deflate=True, garbage=4, clean=True)
        else:
            doc.close()
            return 0
    except Exception:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        doc.close()
        raise
    doc.close()
    shutil.move(tmp_path, path)
    return total


# ══════════════════════════════════════════════════════════════════════════════
# Pass 4 — image-based PDFs (scanned pages with no text layer)
# ══════════════════════════════════════════════════════════════════════════════

def _page_is_image_only(page) -> bool:
    """True when the page has image blocks but zero text blocks."""
    blocks = page.get_text("rawdict").get("blocks", [])
    return (
        any(b.get("type") == 1 for b in blocks) and
        not any(b.get("type") == 0 for b in blocks)
    )


def clean_image_pdf(src: str, dst: str) -> int:
    """
    Pass 4: handle scanned/image-only PDFs.

    For every page that is a pure image (no text layer), this pass:
      1. OCRs the page using Tesseract via PyMuPDF's built-in integration.
      2. Finds lines whose text matches URL / email patterns.
      3. Renders the page to a high-res pixmap, paints white rectangles
         over every URL line, and writes the cleaned image into a new PDF.

    Pages that already have a text layer are copied unchanged (Passes 1-3
    already handled those).

    Requires: Tesseract installed on the system + Pillow (pip install pillow).
    Windows install: winget install UB-Mannheim.TesseractOCR
    """
    if not HAS_FITZ or not HAS_PIL:
        return 0

    src_doc = fitz.open(src)
    out_doc = fitz.open()
    total   = 0
    any_image_page = False

    for page_num in range(len(src_doc)):
        page = src_doc[page_num]

        if not _page_is_image_only(page):
            # Text-layer page — already cleaned, just copy it
            out_doc.insert_pdf(src_doc, from_page=page_num, to_page=page_num)
            continue

        any_image_page = True

        # ── OCR the page ──────────────────────────────────────────────────────
        url_rects: list[fitz.Rect] = []
        try:
            tp = page.get_textpage_ocr(flags=3, dpi=150, full=True)
            for blk in page.get_text("dict", textpage=tp).get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for ln in blk.get("lines", []):
                    txt = "".join(s.get("text", "") for s in ln.get("spans", []))
                    if _LINE_URL_RE.search(txt):
                        url_rects.append(fitz.Rect(ln["bbox"]))
        except Exception:
            # Tesseract not installed — copy page as-is
            out_doc.insert_pdf(src_doc, from_page=page_num, to_page=page_num)
            continue

        if not url_rects:
            out_doc.insert_pdf(src_doc, from_page=page_num, to_page=page_num)
            continue

        # ── Render page → paint white over URL lines → insert into new PDF ───
        zoom = 2.0                          # render at 2× for clean results
        mat  = fitz.Matrix(zoom, zoom)
        pix  = page.get_pixmap(matrix=mat, alpha=False)

        img  = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        draw = ImageDraw.Draw(img)

        for rect in url_rects:
            draw.rectangle(
                [rect.x0 * zoom - 4, rect.y0 * zoom - 4,
                 rect.x1 * zoom + 4, rect.y1 * zoom + 4],
                fill="white",
            )

        # Write the painted image into the output PDF
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        new_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(page.rect, stream=buf.getvalue())
        total += len(url_rects)

    src_doc.close()

    if not any_image_page:
        out_doc.close()
        return 0                            # no image pages — nothing to do

    with tempfile.NamedTemporaryFile(
            delete=False, suffix=".pdf", dir=str(Path(dst).parent)) as tf:
        tmp = tf.name
    try:
        out_doc.save(tmp, deflate=True, garbage=4, clean=True)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        out_doc.close()
        raise
    out_doc.close()
    shutil.move(tmp, dst)
    return total


# ══════════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════════

def process_file(src: Path, dst: Path, password: str = "") -> tuple[int, int, int, int]:
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Pass 1
    pypdf_n = 0
    if HAS_PYPDF:
        try:
            pypdf_n = clean_with_pypdf(str(src), str(dst), password)
        except PermissionError:
            raise
        except Exception as exc:
            print(f"  [pypdf skipped: {exc}]", file=sys.stderr)
            shutil.copy2(src, dst)
    else:
        shutil.copy2(src, dst)

    # Pass 2
    fitz_n = 0
    if HAS_FITZ:
        try:
            fitz_n = clean_with_fitz(str(dst))
        except Exception as exc:
            print(f"  [fitz pass failed: {exc}]", file=sys.stderr)

    # Pass 3
    auto_n = 0
    if HAS_FITZ:
        try:
            auto_n = break_autodetected_links(str(dst))
        except Exception as exc:
            print(f"  [auto pass failed: {exc}]", file=sys.stderr)

    # Pass 4 — image-only pages (scanned PDFs, requires Tesseract + Pillow)
    ocr_n = 0
    if HAS_FITZ and HAS_PIL:
        try:
            ocr_n = clean_image_pdf(str(dst), str(dst))
        except Exception as exc:
            print(f"  [ocr pass failed: {exc}]", file=sys.stderr)

    return pypdf_n, fitz_n, auto_n, ocr_n


def _output_path(src: Path, out_arg: Path | None, batch: bool) -> Path:
    if out_arg is None:
        return src.with_name(f"{src.stem}_no_links{src.suffix}")
    if batch:
        return out_arg / f"{src.stem}_no_links{src.suffix}"
    return out_arg


def main() -> int:
    p = argparse.ArgumentParser(
        description="Remove every hyperlink from a PDF, keeping the text.")
    p.add_argument("input",  type=Path, help="PDF file or folder of PDFs.")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Output file or folder. Default: <name>_no_links.pdf beside input.")
    p.add_argument("--password", default="", help="Password for encrypted PDFs.")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    src: Path = args.input
    if not src.exists():
        print(f"ERROR: not found: {src}", file=sys.stderr)
        return 2

    # Batch mode
    if src.is_dir():
        pdfs = sorted(src.rglob("*.pdf"))
        if not pdfs:
            print(f"No PDFs found under {src}", file=sys.stderr)
            return 1
        out_dir = args.output or (src.parent / f"{src.name}_no_links")
        out_dir.mkdir(parents=True, exist_ok=True)
        grand = 0
        for pdf in pdfs:
            dst = out_dir / pdf.relative_to(src)
            dst = dst.with_name(f"{pdf.stem}_no_links{pdf.suffix}")
            try:
                a, b, c, d = process_file(pdf, dst, args.password)
                grand += a + b + c + d
                if not args.quiet:
                    print(f"  {pdf.name}: removed/broke {a+b+c+d} item(s) -> {dst.name}")
            except Exception as exc:
                print(f"  {pdf.name}: ERROR — {exc}", file=sys.stderr)
        if not args.quiet:
            print(f"\nDone. Total: {grand} item(s).")
        return 0

    # Single file
    if src.suffix.lower() != ".pdf":
        print("ERROR: input must be a .pdf file.", file=sys.stderr)
        return 2

    dst = _output_path(src, args.output, batch=False)
    try:
        a, b, c, d = process_file(src, dst, args.password)
    except PermissionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"pypdf pass : removed {a} annotation link(s).")
        print(f"fitz pass  : removed {b} extra link(s).")
        print(f"auto pass  : broke   {c} viewer-auto-detected URL/email(s).")
        print(f"ocr pass   : painted {d} URL line(s) white in image pages.")
        if d == 0 and a + b + c == 0:
            print("  (No links found — if viewer still shows links, install Tesseract for image PDF support)")
            print("  Windows: winget install UB-Mannheim.TesseractOCR")
        print(f"Saved: {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
