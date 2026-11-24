#!/usr/bin/env bash
# Deploys Secbrain to Fly.io and prints the one-tap phone login link. Requires: flyctl installed and `fly auth login` done once.
set -euo pipefail
cd "$(dirname "$0")/.."
APP="${1:-secbrain-$(head -c4 /dev/urandom | xxd -p)}"
sed -i.bak "s/^app = .*/app = \"$APP\"/" fly.toml && rm -f fly.toml.bak
fly apps create "$APP" 2>/dev/null || true
fly volumes create secbrain_data --size 1 --app "$APP" --yes 2>/dev/null || true
fly secrets set BRAIN_PUBLIC_URL="https://$APP.fly.dev" --app "$APP" --stage
fly deploy --app "$APP" --ha=false
echo; echo "Waiting for the login link…"; sleep 8
fly logs --app "$APP" --no-tail 2>/dev/null | grep -A1 "Phone login link" | tail -2
echo "Open the link on your phone once, then Share → Add to Home Screen."
