FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

COPY app/ ./app/
COPY server.py ./

ENV PORT=8080
ENV GOOGLE_CLOUD_LOCATION=us-central1
ENV GOOGLE_GENAI_USE_VERTEXAI=TRUE

EXPOSE 8080

CMD ["uv", "run", "--no-dev", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
