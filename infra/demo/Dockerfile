FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY infra/demo/requirements.lock.txt ./requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt

COPY pyproject.toml README.md ./
COPY apps ./apps
COPY packages ./packages
COPY infra/terraform ./infra/terraform
COPY scripts ./scripts
COPY alembic.ini ./
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8000
