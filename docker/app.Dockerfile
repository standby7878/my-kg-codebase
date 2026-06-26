FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml code-kg-mcp-plan.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

CMD ["codekg", "--help"]
