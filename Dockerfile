# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY pushmore ./pushmore
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim
ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="PushMore" \
      org.opencontainers.image.description="Inbound webhook gateway (GitHub, Slack-style, Apprise, generic JSON, Pushover API) for Pushover" \
      org.opencontainers.image.source="https://github.com/rikardronnkvist/PushMore" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUSHMORE_VERSION=${VERSION} \
    PUSHMORE_CONFIG=/config/config.yaml \
    PORT=8080

RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir /wheels/*.whl \
 && useradd --system --uid 10001 --home-dir /data pushmore \
 && mkdir -p /data /config && chown pushmore /data

USER 10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz', timeout=4)" || exit 1

ENTRYPOINT ["python", "-m", "pushmore"]
