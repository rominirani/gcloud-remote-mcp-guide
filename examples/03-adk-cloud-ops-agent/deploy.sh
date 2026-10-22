#!/usr/bin/env bash
# Deploy the ADK Cloud Ops Agent to Google Cloud Run
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-YOUR_PROJECT_ID}"
if [[ "${PROJECT_ID}" == "YOUR_PROJECT_ID" || "${PROJECT_ID}" == "your-project-id" ]]; then
  echo "Please set GOOGLE_CLOUD_PROJECT to your actual Google Cloud project ID:"
  echo "  export GOOGLE_CLOUD_PROJECT=\"your-project-id\""
  exit 1
fi

REGION="us-central1"
SERVICE_NAME="gcloud-mcp-ops-agent"

echo "Deploying ${SERVICE_NAME} to Cloud Run in project ${PROJECT_ID} (${REGION})..."
echo "Note: Ensure the Cloud Run runtime service account holds roles/mcp.toolUser and least-privilege IAM roles on ${PROJECT_ID}."

gcloud run deploy "${SERVICE_NAME}" \
  --source=. \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --no-allow-unauthenticated \
  --memory=2Gi \
  --cpu=1 \
  --timeout=300 \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=True,ADK_MODEL=gemini-3-flash-preview" \
  --format=json \
  --quiet
