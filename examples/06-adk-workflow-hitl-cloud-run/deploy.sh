#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?Please export GOOGLE_CLOUD_PROJECT}"
REGION="${GOOGLE_CLOUD_REGION:-us-central1}"
ZONE="${GOOGLE_CLOUD_ZONE:-us-central1-a}"
AGENT_ENGINE_ID="${GOOGLE_CLOUD_AGENT_ENGINE_ID:?Please export GOOGLE_CLOUD_AGENT_ENGINE_ID (run setup_session_engine.py first)}"
SERVICE_NAME="${SERVICE_NAME:-gcloud-mcp-hitl-workflow}"
SA_NAME="gcloud-mcp-hitl-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "=== 1. Ensuring Dedicated Service Account: ${SA_EMAIL} ==="
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="ADK HITL Workflow Cloud Run Service Account" \
    --project="${PROJECT_ID}" \
    --quiet
fi

for ROLE in "roles/aiplatform.user" "roles/mcp.toolUser" "roles/compute.instanceAdmin.v1"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet >/dev/null
done

echo "=== 2. Deploying ${SERVICE_NAME} to Cloud Run (${REGION}) ==="
gcloud run deploy "${SERVICE_NAME}" \
  --source=. \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --no-allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_REGION=${REGION},GOOGLE_CLOUD_ZONE=${ZONE},GOOGLE_CLOUD_LOCATION=global,AGENT_ENGINE_LOCATION=${REGION},GOOGLE_CLOUD_AGENT_ENGINE_ID=${AGENT_ENGINE_ID},GOOGLE_GENAI_USE_VERTEXAI=TRUE" \
  --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
echo ""
echo "=============================================================================="
echo "Deployed Cloud Run Service: ${SERVICE_URL}"
echo "=============================================================================="
