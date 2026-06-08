#!/usr/bin/env bash
#
# UptimeBot launcher — checks your .env, then builds and starts the bot.
# Usage:  ./run.sh
#
set -euo pipefail
cd "$(dirname "$0")"

# 1. Make sure .env exists.
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from the template."
  echo "-> Open .env and fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN, KUMA_USERNAME,"
  echo "   and KUMA_PASSWORD, then run ./run.sh again."
  exit 1
fi

# 2. Read a value out of .env (strips inline comments / surrounding quotes).
getval() {
  grep -E "^$1=" .env | tail -n1 | cut -d= -f2- \
    | sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          -e 's/^"//' -e 's/"$//'
}

# The placeholder each required key has in .env.example (so we can detect "not filled in").
placeholder_for() {
  case "$1" in
    SLACK_BOT_TOKEN) echo "xoxb-your-bot-token" ;;
    SLACK_APP_TOKEN) echo "xapp-your-app-token" ;;
    KUMA_USERNAME)   echo "your-kuma-username" ;;
    KUMA_PASSWORD)   echo "your-kuma-password" ;;
  esac
}

missing=()
for key in SLACK_BOT_TOKEN SLACK_APP_TOKEN KUMA_USERNAME KUMA_PASSWORD; do
  val="$(getval "$key" || true)"
  if [[ -z "$val" || "$val" == "$(placeholder_for "$key")" ]]; then
    missing+=("$key")
  fi
done

if (( ${#missing[@]} )); then
  echo "These values still need to be filled in .env:"
  for m in "${missing[@]}"; do echo "  - $m"; done
  echo "Edit .env, then run ./run.sh again."
  exit 1
fi

# 3. Make sure Docker + the compose plugin are available.
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker isn't installed or isn't on your PATH."
  echo "Install Docker, or run without it -- see the 'Bare metal' section of README.md."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "The 'docker compose' plugin isn't available."
  echo "Install a recent Docker, or use the old syntax:  docker-compose up -d --build"
  exit 1
fi

# 4. Build, start, and follow the logs.
echo "Building and starting UptimeBot..."
docker compose up -d --build
echo
echo "UptimeBot is running. Now following logs -- look for:"
echo "    UptimeBot is starting (Socket Mode)..."
echo "(Ctrl-C stops following the logs; the bot keeps running.)"
echo "Stop the bot later with:  docker compose down"
echo
docker compose logs -f
