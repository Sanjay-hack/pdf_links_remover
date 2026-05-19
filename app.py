"""
app.py — PDF Link Remover web app
Accepts a PDF upload, strips all links, returns the cleaned PDF for download.
"""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from remove_links import process_file

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024   # 50 MB upload limit


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    if "pdf" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    f = request.files["pdf"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file."}), 400

    password = request.form.get("password", "")

    # Write upload to a temp file
    src_fd, src_path = tempfile.mkstemp(suffix=".pdf")
    dst_fd, dst_path = tempfile.mkstemp(suffix=".pdf")
    os.close(src_fd)
    os.close(dst_fd)

    try:
        f.save(src_path)
        a, b, c, d = process_file(Path(src_path), Path(dst_path), password)

        total = a + b + c + d
        out_name = f.filename.replace(".pdf", "_no_links.pdf")

        with open(dst_path, "rb") as out_f:
            data = out_f.read()

        response = send_file(
            io.BytesIO(data),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=out_name,
        )
        # Expose stats in response headers for the frontend
        response.headers["X-Removed-Annotations"] = str(a)
        response.headers["X-Removed-Fitz"]        = str(b)
        response.headers["X-Broke-AutoLinks"]      = str(c)
        response.headers["X-Cleared-ImageLines"]   = str(d)
        response.headers["X-Total"]                = str(total)
        response.headers["Access-Control-Expose-Headers"] = (
            "X-Removed-Annotations, X-Removed-Fitz, "
            "X-Broke-AutoLinks, X-Cleared-ImageLines, X-Total"
        )
        return response

    except PermissionError:
        return jsonify({"error": "Wrong password or encrypted PDF."}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        Path(src_path).unlink(missing_ok=True)
        Path(dst_path).unlink(missing_ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
