#!/usr/bin/env bash
#
# Launch the MCP server + a cloudflared tunnel in one shot.
#
#   1. Starts cloudflared in the background (native binary if installed,
#      otherwise falls back to `npx cloudflared`).
#   2. Waits for the public trycloudflare.com URL to appear in its output.
#   3. Exports MCP_PUBLIC_BASE_URL=<that URL> so the server advertises the correct
#      `resource` URL in its .well-known/oauth-protected-resource metadata.
#   4. Starts the server in the foreground.
#   5. On Ctrl+C (or exit), kills the tunnel too.
#
# Install options for cloudflared (pick one):
#   • brew install cloudflared          ← recommended: small single binary, no JS runtime
#   • have Node on PATH                 ← script falls back to `npx cloudflared`

set -euo pipefail

# --- pick a cloudflared runner ------------------------------------------------
if command -v cloudflared >/dev/null 2>&1; then
    CLOUDFLARED=(cloudflared)
elif command -v npx >/dev/null 2>&1; then
    CLOUDFLARED=(npx --yes cloudflared)
else
    cat <<EOF >&2
✗ cloudflared not found. Install it with ONE of:

    brew install cloudflared           # recommended (single binary, no Node)
    # or have Node on PATH so this script can fall back to `npx cloudflared`

EOF
    exit 1
fi

PORT="${MCP_PORT:-8080}"
TUNNEL_LOG="$(mktemp -t blockbrain-mcp-tunnel.XXXXXX.log)"
trap 'rm -f "$TUNNEL_LOG"; [[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null || true' EXIT INT TERM

echo "→ starting cloudflared tunnel via: ${CLOUDFLARED[*]}"
"${CLOUDFLARED[@]}" tunnel --url "http://localhost:$PORT" > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

# Wait up to ~30s for the trycloudflare URL to show up in the output.
TUNNEL_URL=""
for _ in $(seq 1 60); do
    TUNNEL_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)
    if [[ -n "$TUNNEL_URL" ]]; then break; fi
    sleep 0.5
done

if [[ -z "$TUNNEL_URL" ]]; then
    echo "✗ tunnel did not come up within 30s. Last log lines:" >&2
    tail -20 "$TUNNEL_LOG" >&2
    exit 1
fi

export MCP_PUBLIC_BASE_URL="$TUNNEL_URL"

cat <<BANNER
────────────────────────────────────────────────────────────────────
  Tunnel:       $TUNNEL_URL
  MCP endpoint: $TUNNEL_URL/mcp        ← paste this in Blockbrain UI
  Well-known:   $TUNNEL_URL/.well-known/oauth-protected-resource/mcp

  Ctrl+C to stop both server and tunnel.
────────────────────────────────────────────────────────────────────
BANNER

# Run the server in the foreground so Ctrl+C closes both (trap cleans the tunnel).
exec uv run blockbrain-mcp
