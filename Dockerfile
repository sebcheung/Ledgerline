FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md LICENSE ./
COPY ledger ./ledger
COPY worker ./worker
COPY dashboard ./dashboard
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "ledger.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
