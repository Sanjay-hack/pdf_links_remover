#!/usr/bin/env bash
set -e

# Install Tesseract OCR (system package, required for image-based PDFs)
apt-get install -y tesseract-ocr tesseract-ocr-eng

# Install Python dependencies
pip install -r requirements.txt
