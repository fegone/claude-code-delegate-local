#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# claude-glm — lanza Claude Code corriendo sobre GLM-5.2 (Z.ai Coding Plan)
#              en vez de Anthropic. Tu `claude` normal (Opus) queda INTACTO.
#
# Activar (una sola vez): pega tu key del Coding Plan de https://z.ai/model-api en
#   ~/.config/zai/api-key   (el script la lee de ahí, chmod 600)
#   o exporta ZAI_API_KEY en tu entorno.
#
# Uso:  claude-glm                 # sesion interactiva sobre GLM-5.2
#       claude-glm -p "arregla X"  # cualquier flag de Claude Code pasa igual
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

KEY_FILE="${ZAI_KEY_FILE:-$HOME/.config/zai/api-key}"

if [[ -n "${ZAI_API_KEY:-}" ]]; then
  TOKEN="$ZAI_API_KEY"
elif [[ -f "$KEY_FILE" ]]; then
  TOKEN="$(tr -d '[:space:]' < "$KEY_FILE")"
else
  TOKEN=""
fi

if [[ -z "$TOKEN" || "$TOKEN" == "PEGA_TU_KEY_AQUI" ]]; then
  echo "❌ Falta el API key del Coding Plan de Z.ai." >&2
  echo "   Sácalo de https://z.ai/model-api y guárdalo así:" >&2
  echo "     mkdir -p ~/.config/zai" >&2
  echo "     echo 'TU_KEY' > ~/.config/zai/api-key && chmod 600 ~/.config/zai/api-key" >&2
  echo "   (o: export ZAI_API_KEY=TU_KEY)" >&2
  exit 1
fi

# Redirigir Claude Code a Z.ai (endpoint Anthropic-compatible del Coding Plan)
export ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"
export ANTHROPIC_AUTH_TOKEN="$TOKEN"
# Mapear los tiers de Claude a GLM (1M ctx para opus/sonnet, rapido para haiku)
export ANTHROPIC_DEFAULT_OPUS_MODEL="glm-5.2[1m]"
export ANTHROPIC_DEFAULT_SONNET_MODEL="glm-5.2[1m]"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="glm-4.5-air"
export API_TIMEOUT_MS="3000000"

echo "🟢 Claude Code → GLM-5.2 (Z.ai Coding Plan) · modelo glm-5.2[1m] · plan flat" >&2
exec claude "$@"
