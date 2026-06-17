#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# claude-m — lanza Claude Code corriendo sobre MiniMax M3 en vez de Anthropic.
#            Tu `claude` normal (Opus) queda INTACTO.
#
# Activar (una sola vez): pega tu key de MiniMax (la MISMA que ya usa tu LiteLLM,
#   de https://platform.minimax.io) en  ~/.config/minimax/api-key  (chmod 600)
#   o exporta MINIMAX_API_KEY en tu entorno.
#
# Uso:  claude-m                  # sesion interactiva sobre M3
#       claude-m -p "arregla X"   # cualquier flag de Claude Code pasa igual
#
# Nota: el endpoint /anthropic de MiniMax reporta 200K de contexto (no el 1M real),
#       asi que Claude Code autocompacta ~167K. Limitacion conocida del proveedor.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

KEY_FILE="${MINIMAX_KEY_FILE:-$HOME/.config/minimax/api-key}"

if [[ -n "${MINIMAX_API_KEY:-}" ]]; then
  TOKEN="$MINIMAX_API_KEY"
elif [[ -f "$KEY_FILE" ]]; then
  TOKEN="$(tr -d '[:space:]' < "$KEY_FILE")"
else
  TOKEN=""
fi

if [[ -z "$TOKEN" || "$TOKEN" == "PEGA_TU_KEY_AQUI" ]]; then
  echo "❌ Falta el API key de MiniMax." >&2
  echo "   (es la misma que ya usa tu LiteLLM — de https://platform.minimax.io)" >&2
  echo "     mkdir -p ~/.config/minimax" >&2
  echo "     echo 'TU_KEY' > ~/.config/minimax/api-key && chmod 600 ~/.config/minimax/api-key" >&2
  echo "   (o: export MINIMAX_API_KEY=TU_KEY)" >&2
  exit 1
fi

# Redirigir Claude Code a MiniMax (endpoint Anthropic-compatible)
export ANTHROPIC_BASE_URL="https://api.minimax.io/anthropic"
export ANTHROPIC_AUTH_TOKEN="$TOKEN"
export ANTHROPIC_DEFAULT_OPUS_MODEL="MiniMax-M3"
export ANTHROPIC_DEFAULT_SONNET_MODEL="MiniMax-M3"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="MiniMax-M3"
export API_TIMEOUT_MS="3000000"

echo "🟢 Claude Code → MiniMax M3 (api.minimax.io/anthropic) · ojo: ctx efectivo ~200K" >&2
exec claude "$@"
