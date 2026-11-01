# The team server on its own, for a server you rent (see deploy/README.md). Build it from the
# DevDash-BE folder: docker compose -f deploy/docker-compose.yml up -d --build
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 DEVDASH_HOME=/data
RUN useradd --create-home --uid 10001 devdash && mkdir /data && chown devdash /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app

USER devdash
VOLUME /data
EXPOSE 8770
# Caddy (the other container) gives it HTTPS; it listens inside Docker's own network only.
CMD ["sh", "-c", "exec devdash host --tunnel domain --domain \"$DOMAIN\" --no-caddy --bind 0.0.0.0"]
