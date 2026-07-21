# Knowledge Desk read-only MCP image (uv-managed environment).
# Canonical vault content is mounted at /vault (prefer :ro).
FROM ghcr.io/astral-sh/uv:0.11.29-python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    KNOWLEDGE_DESK_ROOT=/vault \
    KNOWLEDGE_DESK_INDEX_PATH=/tmp/knowledge-desk-index.sqlite \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --locked --no-dev

# Disposable index may be rebuilt inside the container; vault mount should stay read-only.
VOLUME ["/vault"]
EXPOSE 8000

# Default: SSE/streamable HTTP for remote clients. Override for stdio if needed.
CMD ["uv", "run", "--offline", "--no-sync", "knowledge-desk", "--vault", "/vault", "mcp", "serve", "--transport", "sse", "--host", "0.0.0.0", "--port", "8000"]
