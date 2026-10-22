FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID \
    GOOGLE_CLOUD_LOCATION=global \
    GOOGLE_GENAI_USE_VERTEXAI=True \
    PORT=8080

WORKDIR /app

# Notice: No 1GB+ google-cloud-sdk installation needed!
# Commands execute remotely on https://cloudcli.googleapis.com/mcp
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN uv sync --no-dev

COPY app/ ./app/

EXPOSE 8080

CMD ["uv", "run", "adk", "api_server", "--host", "0.0.0.0", "--port", "8080", "."]
