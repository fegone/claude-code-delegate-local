# claude-code-delegate-local

> 🇪🇸 Español · [🇬🇧 English](README.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io/)

**Servidor MCP que delega subagentes de Claude Code a backends alternativos** — modelos locales (LM Studio, llama.cpp, Ollama, vLLM, LiteLLM), DeepSeek, AWS Bedrock, o cualquier endpoint compatible con OpenAI/Anthropic — sin perder tu sesión de orquestador en Claude Code.

Pensado para usuarios que quieren mantener su sesión principal de Claude Code en Anthropic (plan Max o API) para orquestación, mientras descargan subagentes específicos a backends más baratos, más rápidos o que cumplen requisitos de privacidad (HIPAA, datos sensibles, offline).

---

## Índice

- [¿Para qué sirve?](#para-qué-sirve)
- [Características](#características)
- [Instalación rápida](#instalación-rápida)
- [Configuración](#configuración)
- [Herramientas expuestas](#herramientas-expuestas)
- [Búsqueda de agentes en 3 niveles](#búsqueda-de-agentes-en-3-niveles)
- [Routing automático según el modelo](#routing-automático-según-el-modelo)
- [Soporte para modo "thinking"](#soporte-para-modo-thinking)
- [Ejemplo: proxy LiteLLM](#ejemplo-proxy-litellm)
- [Modelos validados](#modelos-validados)
- [Documentación adicional](#documentación-adicional)
- [Advertencias](#advertencias)
- [Licencia](#licencia)

---

## ¿Para qué sirve?

Imaginate que estás trabajando con Claude Code en un proyecto y quieres:

- Que un subagente específico (por ejemplo `security-engineer`) corra en un **modelo local** para ahorrar tokens de tu plan Max, o porque trabajas con datos sensibles que no pueden salir de tu máquina.
- Que otro subagente vaya a **DeepSeek** porque es 10× más barato y rápido para tareas grandes.
- Que tu sesión principal de Claude Code **siga funcionando exactamente igual** — sin cambiar de comando, sin abrir un CLI alternativo, sin perder el plan Max.

Eso es lo que hace `delegate-local`. Es un servidor MCP que se instala una sola vez y expone herramientas que tu orquestador puede invocar para enrutar subagentes específicos a cualquier backend que configures.

## Características

- ✅ **Tu plan Anthropic Max queda intacto.** Sin lanzar CLI separado tipo `ccr code`, sin cambiar comandos.
- ✅ **Búsqueda de agentes en 3 niveles.** El mismo comando funciona en cualquier proyecto — encuentra `.claude/agents/<nombre>.md` del proyecto primero, luego `.claude/skills/<nombre>/SKILL.md`, luego el global en `~/.claude/agents/<nombre>.md`.
- ✅ **Routing dual.** Auto-detecta si el modelo va a `/v1/messages` (formato Anthropic) o `/v1/chat/completions` (formato OpenAI) según el prefijo. Funciona con el modo "thinking" de DeepSeek de cajón.
- ✅ **Tool calling completo.** Los agentes delegados tienen `read_file`, `write_file` y `run_bash` con la misma semántica de loop que los subagentes nativos de Claude Code.

## Instalación rápida

Requiere [uv](https://github.com/astral-sh/uv) y Claude Code.

```bash
git clone https://github.com/fegone/claude-code-delegate-local.git
cd claude-code-delegate-local
uv sync

# Registrar como MCP de Claude Code (scope user = global en todos los proyectos)
claude mcp add delegate-local \
  --scope user \
  --env DELEGATE_LOCAL_URL=http://localhost:4000/v1/messages \
  --env DELEGATE_LOCAL_KEY=tu-api-key-del-backend \
  --env DELEGATE_LOCAL_MODEL=local-qwen-3-6-35b \
  -- uv run --directory $(pwd) python server.py
```

Reinicia Claude Code. El MCP expone 4 herramientas (ver abajo).

## Configuración

Todas las variables de entorno son opcionales; los valores por defecto asumen un proxy LiteLLM en `localhost:4000`.

| Variable | Default | Descripción |
|---|---|---|
| `DELEGATE_LOCAL_URL` | `http://localhost:4000/v1/messages` | URL base del backend. Para modelos en formato OpenAI, el servidor reescribe automáticamente `/v1/messages` → `/v1/chat/completions`. |
| `DELEGATE_LOCAL_KEY` | `""` (vacío) | Bearer token / API key. Se envía como `x-api-key` y `Authorization: Bearer <key>`. |
| `DELEGATE_LOCAL_MODEL` | `local-qwen-3-6-35b` | Alias del modelo por defecto cuando el caller no especifica uno. |
| `DELEGATE_LOCAL_AGENTS_DIR` | `~/.claude/agents` | Dónde buscar las definiciones de agentes globales. |

Ver [docs/CONFIGURATION.md](docs/CONFIGURATION.md) para detalles completos y ejemplos de configuración con LiteLLM, llama.cpp, Ollama, DeepSeek directo y AWS Bedrock.

## Herramientas expuestas

| Herramienta | Para qué sirve |
|---|---|
| `delegate_to_local_agent(agent_name, task, workdir, max_turns, model)` | Ejecuta un agente definido en un `.md` contra el backend default, con tool calling completo |
| `delegate_to_provider(provider_url, api_key, model, agent_name, task, ...)` | Ejecuta un agente contra un endpoint arbitrario (DeepSeek, OpenRouter, etc.) |
| `list_local_agents()` | Lista los agentes en `DELEGATE_LOCAL_AGENTS_DIR` con su metadata |
| `local_backend_status()` | Health check + lista de modelos disponibles en el backend |

## Búsqueda de agentes en 3 niveles

Cuando llamas `delegate_to_local_agent("webdev", ...)` con un `workdir`, el servidor busca la definición del agente en este orden:

1. `<workdir>/.claude/agents/webdev.md` — **agente del proyecto** (máxima prioridad)
2. `<workdir>/.claude/skills/webdev/SKILL.md` — **skill del proyecto** (ubicación alternativa para proyectos tipo SKILL)
3. `~/.claude/agents/webdev.md` — **agente global** (fallback)

Gana el primero que encuentre. La respuesta incluye `agent_source` para que el orquestador sepa qué scope se cargó. Eso permite que el mismo `delegate_to_local_agent("webdev", ...)` funcione en **cualquier** proyecto, recogiendo automáticamente las versiones específicas del proyecto cuando existen.

## Routing automático según el modelo

Los modelos con estos prefijos se enrutan a OpenAI `/v1/chat/completions`:

- `deepseek-*`
- `openai-*`
- `gpt-*`
- `qwen-*` (APIs externas de Qwen — los alias `local-qwen-*` van por Anthropic `/v1/messages`)

Todo lo demás va a Anthropic `/v1/messages`. Internamente todo se normaliza a content blocks estilo Anthropic (text / tool_use / thinking) para que el loop del agente sea uniforme.

## Soporte para modo "thinking"

Para modelos que emiten `reasoning_content` (DeepSeek V4, OpenAI estilo o1), el servidor lo preserva como un content block `{"type": "thinking", "thinking": "..."}` entre turns. Esto es **requerido** por LiteLLM y la mayoría de providers — si descartas el `reasoning_content` del mensaje del assistant en multi-turn, el siguiente request falla con `400 Bad Request`.

`max_tokens` está en 32768 por defecto para darle al modo thinking suficiente espacio tanto para razonar como para emitir contenido.

## Ejemplo: proxy LiteLLM

`litellm/config.yaml` mínimo para usar con este MCP:

```yaml
model_list:
  - model_name: local-qwen-3-6-35b
    litellm_params:
      model: openai/Qwen3-6-35B
      api_base: http://localhost:8000/v1   # tu servidor llama.cpp / vLLM
      api_key: sk-no-key-required

  - model_name: deepseek-v4-flash
    litellm_params:
      model: deepseek/deepseek-chat
      api_key: os.environ/DEEPSEEK_API_KEY

  - model_name: bedrock-sonnet-4-6
    litellm_params:
      model: bedrock/anthropic.claude-sonnet-4-6-20260101-v1:0
      aws_region_name: us-east-1
```

Luego corre `litellm --config config.yaml --port 4000` y apunta este MCP ahí.

## Modelos validados

| Backend | Modelo | Single-turn | Multi-turn |
|---|---|:-:|:-:|
| LiteLLM + llama.cpp | `local-qwen-3-6-35b` (Qwen3.6 35B-A3B) | ✅ | ✅ |
| LiteLLM + DeepSeek API | `deepseek-v4-pro` | ✅ | ✅ |
| LiteLLM + DeepSeek API | `deepseek-v4-flash` | ✅ | ✅ |
| LiteLLM + AWS Bedrock | `bedrock-sonnet-4-6`, `bedrock-llama4-*` | ✅ | ✅ |

Tareas de validación: revisión de SQL injection (agente security-engineer), calculadora HTML (agente creative, 500-800 LOC monolítico), juego de Pac-Man (884 LOC monolítico de un solo shot).

## Documentación adicional

- 📐 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — cómo funciona internamente, diagramas, decisiones de diseño
- ⚙️ [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — todas las variables, setups de LiteLLM/llama.cpp/Ollama/DeepSeek/Bedrock
- 💡 [docs/EXAMPLES.md](docs/EXAMPLES.md) — casos de uso end-to-end con código
- 🤝 [CONTRIBUTING.md](CONTRIBUTING.md) — cómo contribuir
- 📝 [CHANGELOG.md](CHANGELOG.md) — historial de versiones

## Advertencias

- **`run_bash` corre comandos shell dentro de `workdir` sin sandbox.** Confía en los agentes que delegues. Si delegas a un agente público no auditado, la herramienta puede leer/escribir donde el usuario que invoca tenga acceso. No hay aislamiento Docker por defecto.
- **Caps**: `read_file` devuelve los primeros 8KB, `run_bash` corta stdout a 4KB y stderr a 2KB, timeout 120s.
- **`max_turns` hard cap es 40.** Orquestaciones largas deberían diseñarse como múltiples llamadas delegate en vez de un loop único.

## Licencia

MIT. Ver [LICENSE](LICENSE).
