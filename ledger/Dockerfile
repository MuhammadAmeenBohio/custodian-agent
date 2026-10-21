FROM python:3.12.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ledger/app.py

# Do not run as root inside the container
RUN useradd --system --no-create-home ledger
USER ledger

EXPOSE 8000
CMD ["uvicorn", "ledger.app:app", "--host", "0.0.0.0", "--port", "8000"]
