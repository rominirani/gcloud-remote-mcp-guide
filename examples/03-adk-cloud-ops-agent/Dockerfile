FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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

COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "google-adk[mcp,gcp]>=1.0.0" "google-auth>=2.35.0" "pydantic>=2.9.0"

COPY app/ ./app/

EXPOSE 8080

CMD ["adk", "api_server", "--host", "0.0.0.0", "--port", "8080", "."]
