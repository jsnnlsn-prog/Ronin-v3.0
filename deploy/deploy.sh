#!/usr/bin/env bash
# RONIN v3.0 — One-Command GCP Deploy Script
# Usage: ./deploy/deploy.sh [PROJECT_ID] [REGION]
# Example: ./deploy/deploy.sh my-gcp-project us-central1

set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${2:-us-central1}"
SERVICE_NAME="ronin-api"
REPO_NAME="ronin"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${SERVICE_NAME}:latest"
DATA_BUCKET="ronin-data-${PROJECT_ID}"

if [[ -z "${PROJECT_ID}" ]]; then
    echo "❌ Error: No GCP project specified and none configured."
    echo "Usage: ./deploy/deploy.sh [PROJECT_ID] [REGION]"
    exit 1
fi

echo "🚀 RONIN Phase 5 — GCP Deployment"
echo "   Project: ${PROJECT_ID}"
echo "   Region:  ${REGION}"
echo "   Service: ${SERVICE_NAME}"
echo ""

# ─── Prerequisites Check ──────────────────────────────────────────────────

echo "📋 Checking prerequisites..."
for cmd in gcloud docker; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "❌ Required tool not found: $cmd"
        exit 1
    fi
done
echo "✅ Prerequisites OK"

# ─── Enable APIs ─────────────────────────────────────────────────────────

echo ""
echo "🔧 Enabling required GCP APIs..."
gcloud services enable \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    storage.googleapis.com \
    cloudbuild.googleapis.com \
    --project="${PROJECT_ID}"
echo "✅ APIs enabled"

# ─── Artifact Registry ────────────────────────────────────────────────────

echo ""
echo "📦 Setting up Artifact Registry..."
if ! gcloud artifacts repositories describe "${REPO_NAME}" \
        --location="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud artifacts repositories create "${REPO_NAME}" \
        --repository-format=docker \
        --location="${REGION}" \
        --description="RONIN container images" \
        --project="${PROJECT_ID}"
    echo "   Created repository: ${REPO_NAME}"
else
    echo "   Repository already exists: ${REPO_NAME}"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# ─── Secrets ─────────────────────────────────────────────────────────────

echo ""
echo "🔐 Setting up Secret Manager secrets..."

_create_secret_if_missing() {
    local secret_name="$1"
    local default_value="$2"
    if ! gcloud secrets describe "${secret_name}" --project="${PROJECT_ID}" &>/dev/null; then
        echo -n "${default_value}" | gcloud secrets create "${secret_name}" \
            --data-file=- --project="${PROJECT_ID}"
        echo "   Created secret: ${secret_name} (using default — CHANGE IN PRODUCTION)"
    else
        echo "   Secret exists: ${secret_name}"
    fi
}

_create_secret_if_missing "ronin-jwt-secret" "ronin-prod-jwt-secret-$(openssl rand -hex 16)"
_create_secret_if_missing "ronin-vault-key" "$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# Anthropic API key — prompt if not set
if ! gcloud secrets describe "anthropic-api-key" --project="${PROJECT_ID}" &>/dev/null; then
    echo ""
    read -rsp "   Enter ANTHROPIC_API_KEY (will be stored in Secret Manager): " ANTHROPIC_KEY
    echo ""
    echo -n "${ANTHROPIC_KEY}" | gcloud secrets create "anthropic-api-key" \
        --data-file=- --project="${PROJECT_ID}"
else
    echo "   Secret exists: anthropic-api-key"
fi
echo "✅ Secrets configured"

# ─── Storage Bucket ───────────────────────────────────────────────────────

echo ""
echo "🗄️  Setting up Cloud Storage bucket for persistent data..."
if ! gsutil ls "gs://${DATA_BUCKET}" &>/dev/null; then
    gsutil mb -p "${PROJECT_ID}" -l "${REGION}" "gs://${DATA_BUCKET}"
    gsutil lifecycle set /dev/stdin "gs://${DATA_BUCKET}" << 'LIFECYCLE'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 90}}]}
LIFECYCLE
    echo "   Created bucket: gs://${DATA_BUCKET}"
else
    echo "   Bucket exists: gs://${DATA_BUCKET}"
fi
echo "✅ Storage configured"

# ─── Build & Push Image ───────────────────────────────────────────────────

echo ""
echo "🏗️  Building Docker image..."
docker build -f deploy/Dockerfile.prod -t "${IMAGE}" .
echo "   Pushing image..."
docker push "${IMAGE}"
echo "✅ Image pushed: ${IMAGE}"

# ─── Deploy to Cloud Run ─────────────────────────────────────────────────

echo ""
echo "☁️  Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
    --image="${IMAGE}" \
    --region="${REGION}" \
    --platform=managed \
    --allow-unauthenticated \
    --port=8742 \
    --memory=512Mi \
    --cpu=1 \
    --min-instances=0 \
    --max-instances=10 \
    --set-env-vars="RONIN_LOG_LEVEL=INFO,RONIN_HOME=/data/.ronin" \
    --set-secrets="RONIN_JWT_SECRET=ronin-jwt-secret:latest,RONIN_VAULT_KEY=ronin-vault-key:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest" \
    --project="${PROJECT_ID}"

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region="${REGION}" --project="${PROJECT_ID}" \
    --format="value(status.url)")

echo ""
echo "═══════════════════════════════════════════════════"
echo "✅ RONIN deployed successfully!"
echo ""
echo "   Service URL: ${SERVICE_URL}"
echo "   Health check: ${SERVICE_URL}/api/health"
echo ""
echo "   Default admin credentials:"
echo "     Username: admin"
echo "     Password: ronin-admin-change-me"
echo "     ⚠️  CHANGE THESE IMMEDIATELY via /api/auth/register"
echo "═══════════════════════════════════════════════════"
