# Build context is the REPO ROOT (see docker-compose.yml), because the backend imports shared/iban.py.
# backend/Dockerfile.dockerignore lets in only the files copied below: no .env, no invoice_data, no generated/.
FROM python:3.12.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

# Tesseract reads the invoice text (reader 1 of 2). Same Debian package the experiments measured.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app.py backend/authorize.py backend/deps.py backend/facts.py backend/pipeline.py \
     backend/readers.py backend/routing.py backend/store.py backend/summary.py backend/tracing.py backend/
COPY shared/iban.py shared/

# Do not run as root inside the container
RUN useradd --system --no-create-home backend
USER backend

EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
