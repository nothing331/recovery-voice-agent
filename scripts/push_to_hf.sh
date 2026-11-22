#!/usr/bin/env bash
# Upload the app to a Hugging Face Space.
# Usage: scripts/push_to_hf.sh <hf-username>/<space-name>
# Needs `hf auth login` first (a token with write access), plus uv and npm locally.
#
# Docker Spaces need billing on Hugging Face, so this targets a free Gradio-SDK Space: HF runs
# `python app.py` (deploy/hf_app.py), which starts the FastAPI app. The dashboard is built here and
# uploaded prebuilt. Only what the app needs is uploaded: no .env, database, docs or git history.
set -euo pipefail

SPACE="${1:?usage: scripts/push_to_hf.sh <user>/<space>}"
GRADIO_VERSION="6.32.0"  # HF installs this; checked to resolve alongside backend/uv.lock
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "Building the dashboard ..."
(cd "$ROOT/frontend" && npm run build >/dev/null)

mkdir -p "$STAGE/backend" "$STAGE/frontend"
cp "$ROOT/deploy/hf_app.py" "$STAGE/app.py"
rsync -a --exclude '__pycache__' --exclude '*.db' --exclude '.env' "$ROOT/backend/app" "$STAGE/backend/"
cp -R "$ROOT/frontend/dist" "$STAGE/frontend/dist"
(cd "$ROOT/backend" && uv export --frozen --no-hashes --no-dev --no-emit-project --no-header -q \
  -o "$STAGE/requirements.txt")

# Spaces read their config from README front matter.
cat > "$STAGE/README.md" <<EOF
---
title: Autopay Recovery Voice Agent
emoji: 📞
colorFrom: blue
colorTo: green
sdk: gradio
sdk_version: $GRADIO_VERSION
python_version: "3.12"
app_file: app.py
pinned: false
---

Demo voice agent that calls a customer about a failed autopay. The dashboard is password-protected;
keys are set as Space secrets.
EOF

echo "Uploading $(find "$STAGE" -type f | wc -l | tr -d ' ') files to spaces/$SPACE ..."
hf upload "$SPACE" "$STAGE" . --repo-type space --delete "*" --commit-message "Deploy from local checkout"
echo "Done: https://huggingface.co/spaces/$SPACE"
