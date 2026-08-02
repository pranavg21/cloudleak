#!/usr/bin/env bash
#
# Start CloudLeak for a demo: mints an API key, wires it into both halves,
# starts the backend and frontend, and shuts both down cleanly on Ctrl-C.
#
#   ./run.sh
#
# First run installs dependencies, so give it a couple of minutes. After that
# it starts in seconds.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

say() { printf "\n\033[1m%s\033[0m\n" "$1"; }
fail() { printf "\n\033[31m%s\033[0m\n" "$1" >&2; exit 1; }

command -v python3 >/dev/null || fail "python3 is not installed."
command -v node >/dev/null || fail "Node.js is not installed. Get it from nodejs.org."

# --- backend setup ------------------------------------------------------------
say "Setting up the backend..."
cd "$ROOT/backend"

if [ ! -d venv ]; then
  python3 -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Mint a key for this run and derive its digest. The plaintext goes to the
# frontend's server-side env; only the digest goes to the backend.
say "Minting an API key for this session..."
API_KEY="cl_demo_$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
API_KEY_HASH="$(python3 -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$API_KEY")"

# --- frontend setup -----------------------------------------------------------
cd "$ROOT/frontend"
cat > .env.local <<EOF
# Written by run.sh. Regenerated on every run.
CLOUDLEAK_API_BASE_URL=http://localhost:${BACKEND_PORT}
CLOUDLEAK_API_KEY=${API_KEY}
EOF

if [ ! -d node_modules ]; then
  say "Installing frontend dependencies (first run only, takes a minute)..."
  npm install --no-audit --no-fund
fi

# --- launch -------------------------------------------------------------------
cleanup() {
  printf "\n\033[1mShutting down...\033[0m\n"
  [ -n "${BACKEND_PID:-}" ] && kill "$BACKEND_PID" 2>/dev/null || true
  [ -n "${FRONTEND_PID:-}" ] && kill "$FRONTEND_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

say "Starting the audit engine on port ${BACKEND_PORT}..."
cd "$ROOT/backend"
CLOUDLEAK_API_KEY_HASHES="$API_KEY_HASH" \
CLOUDLEAK_ALLOWED_ORIGINS="http://localhost:${FRONTEND_PORT}" \
  ./venv/bin/uvicorn main:app --port "$BACKEND_PORT" > /tmp/cloudleak-backend.log 2>&1 &
BACKEND_PID=$!

for _ in $(seq 1 40); do
  if curl -sf "http://localhost:${BACKEND_PORT}/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    cat /tmp/cloudleak-backend.log
    fail "The backend failed to start. Log above."
  fi
  sleep 1
done

say "Starting the web app on port ${FRONTEND_PORT}..."
cd "$ROOT/frontend"
npm run dev -- --port "$FRONTEND_PORT" > /tmp/cloudleak-frontend.log 2>&1 &
FRONTEND_PID=$!

for _ in $(seq 1 60); do
  if curl -sf "http://localhost:${FRONTEND_PORT}" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
    cat /tmp/cloudleak-frontend.log
    fail "The frontend failed to start. Log above."
  fi
  sleep 1
done

cat <<EOF

  CloudLeak is running.

    Demo this:      http://localhost:${FRONTEND_PORT}
    API docs:       http://localhost:${BACKEND_PORT}/docs
    Sample files:   ${ROOT}/samples/

  Drag samples/azure_cost_export.csv onto the page to run an audit.

  Logs: /tmp/cloudleak-backend.log, /tmp/cloudleak-frontend.log
  Press Ctrl-C to stop both.

EOF

wait
