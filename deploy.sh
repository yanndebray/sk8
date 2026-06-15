#!/usr/bin/env bash
#
# Provision one sk8 agent on GCP Cloud Run and print the end-user's
# `claude mcp add` command. See docs/cloud-deployment.md for the full writeup
# (granularity choices, the self-serve control-plane pattern, security notes).
#
# Usage:
#   ./deploy.sh <agent-id> [--build]      e.g.  ./deploy.sh iris --build
#   ./deploy.sh suggest [N]               propose N agent names (default 6)
#
# <agent-id> is the isolation + lifecycle boundary for one agent instance — it
# names the Cloud Run service, the bearer-token secret, and the state dir. Pick
# the granularity you want to isolate at (per end-user / per team / per session)
# — see docs/cloud-deployment.md §4 "Choosing the agent granularity".
#
# Env (override as needed):
#   PROJECT   GCP project id            (default: gcloud's active project)
#   REGION    Cloud Run region          (default: us-central1)
#   REPO      Artifact Registry repo    (default: agents)
#   PROFILE   agent profile dir         (default: none -> default profile)
#             e.g. PROFILE=./profiles/data-analyst ./deploy.sh iris --build
#             bakes extra Python deps, skills, and a tool/system-prompt spec
#             into the image. Distinct profiles get distinct image tags.
#
# One-time project setup (run once, not per agent):
#   gcloud services enable run.googleapis.com secretmanager.googleapis.com \
#     artifactregistry.googleapis.com cloudbuild.googleapis.com
#   gcloud artifacts repositories create agents \
#     --repository-format=docker --location=us-central1
#   # Store the shared Claude credential the agents run under:
#   printf '%s' "$ANTHROPIC_API_KEY" | \
#     gcloud secrets create anthropic-api-key --data-file=-
#
set -euo pipefail

# --- Agent-name generation (adjective-noun slugs, e.g. lunar-iris) -----------
ADJECTIVES=(amber azure brave bright calm clever cosmic crimson dapper eager
  fuzzy gentle golden hazel jolly lunar mellow nimble plucky quiet rusty silver
  sleek snowy solar stellar sunny swift teal vivid witty zesty)
NOUNS=(otter heron lynx falcon willow cedar iris fern comet nebula quartz pebble
  maple robin sparrow badger ferret marmot koi newt wren finch tansy clover sage
  basil juniper aspen birch beacon)

suggest_names() {
  local n="${1:-6}" i a nn
  echo "Suggested agent names (adjective-noun slugs):" >&2
  for ((i = 0; i < n; i++)); do
    a="${ADJECTIVES[RANDOM % ${#ADJECTIVES[@]}]}"
    nn="${NOUNS[RANDOM % ${#NOUNS[@]}]}"
    echo "  ${a}-${nn}" >&2
  done
}

# --- Slugify + validate an agent id ------------------------------------------
slugify() {
  # lowercase; collapse any run of non-alphanumerics to a single hyphen; trim.
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/-/g; s/-+/-/g; s/^-+//; s/-+$//'
}

validate_agent_id() {
  # Cloud Run service names: start with a letter, then [a-z0-9-], no trailing
  # hyphen, <=63 chars. We cap at 40 to leave headroom and stay readable.
  local id="$1"
  if [[ ! "$id" =~ ^[a-z]([-a-z0-9]*[a-z0-9])?$ ]]; then
    echo "ERROR: agent id '$id' is invalid — must start with a letter and use only a-z, 0-9, hyphens." >&2
    echo "       Try: ./deploy.sh suggest" >&2
    return 1
  fi
  if [ "${#id}" -gt 40 ]; then
    echo "ERROR: agent id '$id' is too long (${#id} chars; max 40)." >&2
    return 1
  fi
}

# --- Subcommand: propose names and exit --------------------------------------
if [ "${1:-}" = "suggest" ] || [ "${1:-}" = "--suggest" ]; then
  suggest_names "${2:-6}"
  exit 0
fi

# --- Resolve the agent id ----------------------------------------------------
RAW_ID="${1:?usage: ./deploy.sh <agent-id> [--build]   (or: ./deploy.sh suggest)}"
BUILD="${2:-}"

AGENT_ID="$(slugify "$RAW_ID")"
validate_agent_id "$AGENT_ID"
if [ "$AGENT_ID" != "$RAW_ID" ]; then
  echo ">> normalized agent id: '$RAW_ID' -> '$AGENT_ID'" >&2
fi

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-agents}"
PROFILE="${PROFILE:-}"
SERVICE="$AGENT_ID"
TOKEN_SECRET="$AGENT_ID-token"

# The profile (if any) sets the image tag: same profile -> one image, distinct
# profiles -> distinct images. No profile -> the default :latest image.
if [ -n "$PROFILE" ]; then
  [ -d "$PROFILE" ] || { echo "ERROR: profile dir '$PROFILE' not found." >&2; exit 1; }
  TAG="$(slugify "$(basename "$PROFILE")")"
else
  TAG="latest"
fi
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/sk8:$TAG"

echo ">> project=$PROJECT region=$REGION service=$SERVICE profile=${PROFILE:-default}" >&2

# Build & push the image only when asked (it rarely changes between agents).
if [ "$BUILD" = "--build" ]; then
  echo ">> building image $IMAGE" >&2
  if [ -n "$PROFILE" ]; then
    # --tag mode can't pass --build-arg; route profile builds through
    # cloudbuild.yaml, which forwards PROFILE to `docker build`.
    gcloud builds submit --config cloudbuild.yaml \
      --substitutions="_PROFILE=$PROFILE,_IMAGE=$IMAGE" .
  else
    gcloud builds submit --tag "$IMAGE" .
  fi
fi

# Mint a fresh per-agent bearer token and store it in Secret Manager.
TOKEN="$(openssl rand -hex 32)"
if gcloud secrets describe "$TOKEN_SECRET" >/dev/null 2>&1; then
  printf '%s' "$TOKEN" | gcloud secrets versions add "$TOKEN_SECRET" --data-file=-
else
  printf '%s' "$TOKEN" | gcloud secrets create "$TOKEN_SECRET" --data-file=-
fi

# Grant the Cloud Run runtime service account read access to the secrets it
# mounts. The default runtime identity is the Compute Engine default SA
# (<project-number>-compute@...); without secretAccessor on each secret the
# first deploy fails with "Permission denied on secret". Idempotent.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNTIME_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"
for SECRET in "$TOKEN_SECRET" anthropic-api-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:$RUNTIME_SA" \
    --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
done

# Deploy a dedicated Cloud Run service for this agent.
#   --timeout=3600  : tasks run up to 600s; the 300s default would 504 them
#   --concurrency=8 : NOT for parallel tasks — MCP's streamable-HTTP transport
#                     holds several connections open per session (a long-lived
#                     GET SSE channel + POSTs). concurrency=1 starves the extra
#                     streams (Cloud Run 500s / 429s) and the client can't
#                     connect. run_task stays effectively serial via
#                     max-instances=1. (See docs/cloud-deployment.md "Concurrency".)
#   --allow-unauthenticated : the FastMCP bearer token is the gate, not Google IAM
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --allow-unauthenticated \
  --timeout=3600 \
  --cpu=2 --memory=2Gi \
  --min-instances=0 --max-instances=1 --concurrency=8 \
  --set-secrets="AGENT_TOKEN=$TOKEN_SECRET:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest"

# --- Optional: persistent state between runs/sessions (see docs §9) ----------
# Stateless by default (in-memory /tmp wiped each run). To give the agent a
# working tree that survives across runs, attach a SHARED NFS server and scope
# per agent/session by directory. Recommended: a self-managed NFS VM (~$15-25/mo,
# e2-small + a right-sized disk); graduate to Filestore for HA/scale. The
# service must be attached to the NFS server's VPC (Direct VPC egress).
# Uncomment & set NFS_IP to the server's internal IP (VM or Filestore):
#
#   --network=default --subnet=default \
#   --add-volume=name=state,type=nfs,location="$NFS_IP":/export \
#   --add-volume-mount=volume=state,mount-path=/mnt/state \
#   --set-env-vars=AGENT_DEFAULT_CWD="/mnt/state/$AGENT_ID/current/workspace"
#
# Then manage TTL/quota with a Cloud Scheduler-driven reaper (docs §9).

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"

cat >&2 <<EOF

================================================================
 Agent '$AGENT_ID' provisioned. Hand the user exactly this one command:
================================================================

claude mcp add $AGENT_ID $URL/mcp \\
  --transport http \\
  --header "Authorization: Bearer $TOKEN"

EOF
