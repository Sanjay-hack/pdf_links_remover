FROM python:3.11-slim

# Install Tesseract OCR + English language data
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Tell PyMuPDF where tessdata lives on this image
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

CMD gunicorn app:app --timeout 120 --workers 2 --bind 0.0.0.0:$PORT
