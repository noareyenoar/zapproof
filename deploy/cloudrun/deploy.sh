#!/usr/bin/env bash
# Deploy the hosted Zapproof API + remote MCP to Cloud Run.
#
# The owner runs this script. It builds the compact database, builds the image with
# Cloud Build, deploys one Cloud Run service, optionally deploys the static site to
# Firebase Hosting, and adds a Cloud Logging exclusion for request logs.
#
#   bash deploy/cloudrun/deploy.sh
#
# Environment overrides:
#   PROJECT_ID=studio-226725885-31239  REGION=us-central1  SERVICE=zapproof
#   REPO=zapproof                      FIREBASE_SITE=zapproof
#   SKIP_HOSTING=1                     SKIP_LOGGING_EXCLUSION=1
set -euo pipefail

# Git Bash rewrites POSIX-looking command-line arguments into Windows paths before
# the Windows `gcloud` binary sees them. On 2026-11-17 that shipped
# REPINDEX_DB=C:\Program Files\Git\app\data\hosted.db and every database route
# answered 500 while /v1/methodology stayed green. The fix is to never pass that
# path from this script: the image's ENV already points at /app/data/hosted.db.
#
# Do NOT `export MSYS_NO_PATHCONV=1` here. On Git Bash that also stops the gcloud
# launcher from converting its own /c/... install path, so gcloud dies with
# `can't open file 'D:\c\Users\...\gcloud.py'` before the first API call. That was
# the 2026-11-23 deploy failure; path safety now comes from not passing paths.


PROJECT_ID="${PROJECT_ID:-studio-226725885-31239}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-zapproof}"
REPO="${REPO:-zapproof}"
SITE="${FIREBASE_SITE:-zapproof}"
SKIP_HOSTING="${SKIP_HOSTING:-0}"
SKIP_LOGGING_EXCLUSION="${SKIP_LOGGING_EXCLUSION:-0}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

command -v gcloud >/dev/null || { echo "gcloud is required: https://cloud.google.com/sdk/docs/install"; exit 1; }
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/"; exit 1; }

echo "== project=$PROJECT_ID region=$REGION service=$SERVICE"
gcloud config set project "$PROJECT_ID" >/dev/null

echo "== enabling APIs (idempotent)"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT_ID"

if [ ! -f data/hosted.db ]; then
  echo "== building the compact hosted database"
  uv run python scripts/build_hosted_db.py
fi

mkdir -p .deploy
if [ ! -s .deploy/usage_salt ]; then
  uv run python -c 'import secrets; print(secrets.token_hex(32))' > .deploy/usage_salt
  echo "== generated .deploy/usage_salt (keep it: changing it resets caller dedupe)"
fi
USAGE_SALT="$(cat .deploy/usage_salt)"

if ! gcloud artifacts repositories describe "$REPO" \
  --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "== creating Artifact Registry repository $REPO"
  gcloud artifacts repositories create "$REPO" \
    --repository-format=docker --location="$REGION" --project "$PROJECT_ID" \
    --description="Zapproof hosted containers"
fi

TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$SERVICE:$TAG"

echo "== building $IMAGE"
gcloud builds submit --project "$PROJECT_ID" \
  --config deploy/cloudrun/cloudbuild.yaml \
  --substitutions="_REGION=$REGION,_REPO=$REPO,_IMAGE=$SERVICE,_TAG=$TAG" .

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUN_HOST="$SERVICE-$PROJECT_NUMBER.$REGION.run.app"
PUBLIC_URL="https://$RUN_HOST"

echo "== deploying Cloud Run service $SERVICE"
gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --platform managed \
  --image "$IMAGE" --allow-unauthenticated \
  --memory 1Gi --cpu 1 --concurrency 20 \
  --min-instances 0 --max-instances 3 --timeout 60 \
  --set-env-vars "^@^ZAPPROOF_PUBLIC_URL=$PUBLIC_URL@ZAPPROOF_MCP_ALLOWED_HOSTS=$RUN_HOST@ZAPPROOF_TRUST_PROXY=1@ZAPPROOF_MCP_STATELESS=1@ZAPPROOF_MCP_JSON_RESPONSE=1@USAGE_METRICS=1@USAGE_SINK=stdout@USAGE_SALT=$USAGE_SALT"
# REPINDEX_DB is deliberately absent: the image's ENV already points at
# /app/data/hosted.db, and keeping the path out of the gcloud arguments means no
# shell can rewrite it (see MSYS_NO_PATHCONV above).

# Read the URL Google actually assigned instead of trusting the composed name.
URL="$(gcloud run services describe "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
RUN_HOST="${URL#https://}"
gcloud run services update "$SERVICE" \
  --project "$PROJECT_ID" --region "$REGION" \
  --update-env-vars "ZAPPROOF_PUBLIC_URL=$URL,ZAPPROOF_MCP_ALLOWED_HOSTS=$RUN_HOST" >/dev/null

if [ "$SKIP_LOGGING_EXCLUSION" != "1" ]; then
  echo "== excluding Cloud Run request logs (they contain caller paths and addresses)"
  if gcloud logging sinks describe _Default --project "$PROJECT_ID" \
    --format='value(exclusions.name)' 2>/dev/null | grep -qw "zapproof-request-logs"; then
    echo "   exclusion already present"
  else
    gcloud logging sinks update _Default --project "$PROJECT_ID" \
      --add-exclusion='name=zapproof-request-logs,filter=LOG_ID("run.googleapis.com/requests")' \
      || echo "   could not add the exclusion; see docs/deploy_cloudrun.md"
  fi
fi

if [ "$SKIP_HOSTING" != "1" ] && [ -n "$SITE" ]; then
  if command -v firebase >/dev/null; then
    echo "== rendering and deploying the static site to Firebase Hosting ($SITE)"
    uv run python scripts/render_site.py \
      --base-url "https://$SITE.web.app" --api-url "$URL" --out public
    if ! firebase deploy --only "hosting:$SITE" --project "$PROJECT_ID"; then
      # A new Firebase project has only its default site; a named site must be created
      # once before the first deploy.
      echo "== hosting site $SITE not found; creating it once and retrying"
      firebase hosting:sites:create "$SITE" --project "$PROJECT_ID" || true
      firebase deploy --only "hosting:$SITE" --project "$PROJECT_ID"
    fi
  else
    echo "== firebase CLI not found; skipped the static site (npm i -g firebase-tools)"
  fi
fi

echo
echo "== done"
echo "API:   $URL"
echo "MCP:   $URL/mcp"
echo "Skill: $URL/skill.md"
if [ "$SKIP_HOSTING" != "1" ] && [ -n "$SITE" ] && command -v firebase >/dev/null; then
  echo "Site:  https://$SITE.web.app"
fi
echo
echo "Verify:"
echo "  curl -s $URL/v1/health"
echo "  curl -s $URL/skill.md | head"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' -X POST $URL/mcp \\"
echo "    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \\"
echo "    -d '{\"jsonrpc\":\"2.0\",\"method\":\"initialize\",\"id\":1,\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"deploy-check\",\"version\":\"0\"}}}'"
