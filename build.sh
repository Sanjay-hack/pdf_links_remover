#!/usr/bin/env bash
set -e

sudo apt-get install -y tesseract-ocr tesseract-ocr-eng
pip install -r requirements.txt
