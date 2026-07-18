FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml code-kg-mcp-plan.md ./
COPY src ./src
COPY third_party/wheels ./third_party/wheels

RUN pip install --upgrade pip \
    && pip install "numpy>=1.23" \
    && pip install --no-index --find-links=/app/third_party/wheels --no-deps zvec==0.5.1 \
    && pip install . \
    && pip check

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin codekg \
    && mkdir -p /data/zvec \
    && chown -R codekg:codekg /app /data/zvec

USER codekg

CMD ["codekg", "--help"]
