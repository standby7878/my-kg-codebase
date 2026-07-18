FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml code-kg-mcp-plan.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install ".[search]"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin codekg \
    && mkdir -p /data/zvec \
    && chown -R codekg:codekg /app /data/zvec

USER codekg

CMD ["codekg", "--help"]
