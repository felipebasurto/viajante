# Local stdio MCP server for container-based MCP clients and the Docker MCP Catalog.
# Run it attached: docker run -i --rm viajante
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md MANIFEST.in LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]" \
    && useradd --create-home --uid 10001 viajante \
    && mkdir /state \
    && chown viajante /state

USER viajante
# Cooldowns and consent cookies live here. Mount a volume to keep them across runs.
ENV VIAJANTE_STATE_DIR=/state
ENTRYPOINT ["viajante-mcp"]
