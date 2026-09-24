# Auditoría 2026-09-24 — despachos de tareas largas (GLM-5.3 / `glm-coding-plan-think`)

Repo: `claude-delegate-local`, worktree branch `fix/delegate-long-tasks`. Auditoría READ-ONLY (ningún archivo del repo modificado salvo este informe).
Nota previa: `~/.claude/roles/webdev.md` NO existe en esta máquina (error al leer); se continuó con cuidado usando las reglas del propio repo y las instrucciones del despacho.

Contexto: 3 fallos consecutivos de un agente real multi-archivo (~20 test files, migración Postgres) que un subagente Sonnet sin harness resuelve en 90–180 tool calls.

## 1. `HARD_MAX_TURNS = 40` constante no configurable — CONFIRMADO
- `server.py:74` — `HARD_MAX_TURNS = 40` (constante plana, sin `os.getenv`).
- Clamp: `server.py:1646` — `max_turns = max(1, min(max_turns, HARD_MAX_TURNS))`. Docs de la tool repiten el cap: `server.py:2247` ("hard cap 40").
- Resolución default: `server.py:1642-1645` — `max_turns=0` (sentinel) → `LOCAL_MAX_TURNS=25` (`server.py:69`) si `local-*`, si no `DELEGATE_CLOUD_MAX_TURNS` default 25 (`server.py:73`). GLM corrió con 30 y 40 porque el orquestador pasó `max_turns` explícito… y aun así el clamp lo habría cortado en 40.
- Mecanismo del fallo: 90–180 tool calls ÷ ~2–4 calls/turno = 25–60 turnos necesarios; 40 es insuficiente y 25 (default) catastrófico. El corte es abrupto: `server.py:1933-1940` no ejecuta las tools del último turno y responde "hit turn limit still wanting to run: …"; `hit_turn_limit` se calcula en `server.py:2064` (`turn >= max_turns and bool(tool_uses)`).
- Fix propuesto:
  - `HARD_MAX_TURNS = int(os.getenv("DELEGATE_HARD_MAX_TURNS", "150"))` — deja de ser restricción operativa y pasa a guard-rail.
  - Defaults por backend: cloud → `DELEGATE_CLOUD_MAX_TURNS` default **60**; local (`local-*`, `ornith`) → mantener `DELEGATE_LOCAL_MAX_TURNS=25` (los MoE pequeños de oMLX saturan su contexto mucho antes de 40 turnos; el límite los PROTEGE, no los limita artificialmente — ver comentario `server.py:614-618` sobre por qué se comprime).
  - `max_turns` explícito del caller se respeta dentro del nuevo techo (actualizar docstring de la tool, `server.py:2247`).

## 2. `KEEP_TOOL_RESULTS = 6` → `evicted_tool_results` 37–45 — CONFIRMADO (+ bug nuevo)
- `server.py:120` — `KEEP_TOOL_RESULTS = int(os.getenv("DELEGATE_KEEP_TOOL_RESULTS", "6"))` (la env YA existe).
- Evicción: `_evict_old_tool_results` `server.py:1432-1466`; invocada CADA turno en `server.py:1735` (F1b: "podar antes de armar el request").
- Mecanismo: el marker que deja (`server.py:1461-1464`) dice "re-pedila si la necesitás" → el modelo re-lee archivos ENTEROS (un `read_file` ≈ 50K chars, tope `MAX_READ_CHARS=120_000`, `server.py:75`), quemando turnos y re-llenando el contexto que la poda quería ahorrar. Con 37–45 desalojos sobre 40 turnos, la memoria de trabajo efectiva es ~3 turnos. El modelo trabaja amnésico.
- **BUG NUEVO (interacción eviction × dedup)**: el dedup F3 (`server.py:1995-2004`, `seen_calls` creado en `server.py:1718`) cachea `(name, args)` → turno y NUNCA se limpia al desalojar. Después de una evicción, la re-lectura idéntica que el marker pide se DEDUP con el mensaje "su resultado ya está en tu contexto" (`server.py:2000-2003`)… que es FALSO: la evicción lo borró. El modelo queda atrapado: la poda le dice "re-pedila" y el dedup le dice "ya la tenés". Solo escapa cambiando `offset/limit` (que a menudo no hace). Esto explica las re-lecturas repetidas observadas y quema turnos hasta el límite.
- Fix:
  - Limpiar `seen_calls[key]` de toda tool_result desalojada dentro de `_evict_old_tool_results` (o al menos para `read_file`, que es la que el marker invita a repetir). P0.
  - Default por backend: cloud → `DELEGATE_KEEP_TOOL_RESULTS=20`; local → mantener 6.
  - Marker más útil: incluir `path` + rango de líneas del contenido desalojado ("ya leíste server.py:1-140"), no solo el tamaño, para que una eventual re-lectura sea un rango dirigido y no el archivo entero.

## 3. `RUN_BASH_TIMEOUT = 120` vs suite de 160–190 s — CONFIRMADO
- `server.py:595` — `RUN_BASH_TIMEOUT = int(os.getenv("DELEGATE_RUN_BASH_TIMEOUT", "120"))` (env ya existe: `DELEGATE_RUN_BASH_TIMEOUT`).
- Ejecución: `server.py:966-976` — `asyncio.wait_for(proc.communicate(), timeout=RUN_BASH_TIMEOUT)` + `_kill_process_group` + mensaje pidiendo "re-ejecuta una versión acotada del comando, o partes de él".
- Mecanismo: la suite completa NO puede terminar: a los 120 s muere el process group. El agente no puede verificar su trabajo de migración (~20 test files), itera a ciegas o gasta turnos en sharding manual de pytest. Con 40 turnos de techo, cada suite fallida cuesta ~1 turno + el de reintento acotado.
- Fix:
  - Defaults por backend: cloud → `DELEGATE_RUN_BASH_TIMEOUT=600`; local → mantener 120 (los slots locales comparten GPU/event-loop y el semáforo `_BASH_MAX_CONCURRENCY=4`, `server.py:609`, por turno puede retenerlo K×120 s — ver `server.py:1930-1946` y `1944-1955`).
  - Mejor aún: aceptar un `timeout` explícito por llamada en run_bash (schema + clamp `1 <= timeout <= DELEGATE_RUN_BASH_MAX`, default 900 para cloud / 120 local), para que el agente pida más tiempo solo cuando lo necesita ("pytest" no es sharding-friendly).

## 4. Llamada MCP síncrona, log de una línea al final — CONFIRMADO (con un matiz)
- Matiz: SÍ existe `await ctx.report_progress(progress=turn, total=max_turns, message="agent '…' turn N/M")` por turno (`server.py:1748-1753`) y por tarea en batch (`server.py:2412` aprox). Pero (a) muchos clientes MCP no renderizan progress notifications, (b) no hay NADA persistido en disco durante la ejecución, (c) no distingue "trabajando" de "una llamada al backend colgada 20 min" (TURN_TIMEOUT=1800, `server.py:79`).
- Log: `_log_dispatch` `server.py:843-880` escribe UNA línea JSONL al FINAL; `_LOG_PATH = ~/.cache/claude-delegate-local/dispatches.jsonl` (`server.py:827-830`); campos en `_LOG_FIELDS` (`server.py:832-840`, solo metadatos: turns, tool_calls, tokens, cache…).
- Mecanismo: 15+ min sin señal observable ⇒ el orquestador no puede distinguir agente vivo de hang ni matar selectivamente; post-mortem, la única evidencia es la línea final.
- Fix: heartbeat/progreso persistente por dispatch — diseño en §8.

## 5. Informes fechados 2026-07-19 — CONFIRMADO
- El system prompt se arma en `server.py:1671-1691` y NO incluye fecha: solo `MODE:LOCAL`, workdir (`1674`), presupuesto de turnos (`1678`), aviso de no-git (`1683-1686`) y el body del agente (`1689-1691`). El modelo solo puede "saber" la fecha de sus pesos.
- Fix: inyectar junto a Workdir: `Today's date: {time.strftime('%Y-%m-%d')}.` (1 línea, `server.py:1674`).

## 6. Instrucciones de commit en tool results — CONFIRMADO (fix de ebce68a incompleto)
- `server.py:2043-2053`: el aviso de countdown (fires cuando `turns_left <= TURN_WARN_REMAINING=3`, `server.py:156`) para agentes de ESCRITURA dice literalmente, en `server.py:2050`: `Ejecuta AHORA: git add -A && git commit -m "wip: ..."`. Viaja DENTRO del tool_result del último turno (`server.py:2055`), es decir, como el mensaje más reciente — gana contra el system prompt.
- La exención es solo por NOMBRE de agente: `_es_agente_de_solo_lectura` (`server.py:168-170`, patrones `165`) matchea review/audit/explor/investig. Un agente coder/webdev (el caso GLM) la recibe siempre, le hayan dicho o no que no commitee. Eso es exactamente lo que ebce68a dejó afuera.
- Fix: reemplazar el imperativo por "persiste tu estado con write_file (informe en <workdir>) y responde el resumen final" para TODOS los agentes; si se quiere conservar el commit en algún flujo, gatearlo con un flag explícito del dispatch (`allow_commit: bool = False`). Nunca una instrucción de git DENTRO de un tool_result.

## 7. Otros límites que cortan una tarea larga (inventario file:line)

| Límite | Definición | Efecto sobre tarea larga |
|---|---|---|
| `DELEGATE_DISPATCH_TIMEOUT`/`DISPATCH_TIMEOUT=3600` | `server.py:87` | Deadline global; aborta en `server.py:1738-1746` y `1759-1770` si quedan < `DISPATCH_MIN_SLICE=30` s (`server.py:92`). Una tarea de 40 turnos con suite de 3 min ≈ OK, pero con retries HTTP (backoff `server.py:1780-1800`) puede caer. Subir a 5400 para cloud. |
| `TURN_TIMEOUT=1800` | `server.py:79` (env `DELEGATE_TURN_TIMEOUT`) | Un solo turno (1 llamada al backend) >30 min = muerte del dispatch. Razonable; mantener. |
| `BATCH_TASK_TIMEOUT = DISPATCH_TIMEOUT + 900` | `server.py:605-608` | En batch, es techo por tarea; como es dispatch+grace, casi nunca corta primero (bien diseñado). Subir en tándem con DISPATCH_TIMEOUT. |
| `DEFAULT_MAX_TOKENS=65_536` por turno | `server.py:128` (constante, sin env) | Para `glm-coding-plan-think` el presupuesto es explícito: `MODEL_BUDGET_POLICY["glm-coding-plan-think"]=65_536` con thinking 16K (`server.py:373`); cap provider glm- 131072 (`server.py:173`). Un turno con mucho thinking + write_file grande puede cortarse a mitad (`stop_reason=max_tokens` → no-nudge, `server.py:1915-1922`); el F4 detecta args truncados (`server.py:1961-1967`). Exponer env `DELEGATE_DEFAULT_MAX_TOKENS` y subir think a 131072 si se observan cortes. |
| Truncado stdout/stderr de run_bash | `server.py:982-989` (12_000 / 4_000 chars) | Salida de suite de 20 archivos se corta; el agente puede no ver el fallo real. Subir a 20K/8K para cloud o paginar. |
| Truncado de `read_file` | `MAX_READ_CHARS=120_000` (`server.py:75`); mensaje "Continúa con offset" (`server.py:1056`) | OK, pero empeora el costo de re-lecturas post-evicción (§2). |
| Truncado de input de tool args | `server.py:1209-1225` + `_input_truncated` (`server.py:1961-1967`) | JSON cortado en tránsito → llamada marcada malformed; gasta turno. |
| `MAX_COMPLETION_NUDGES=1` | `server.py:110` | Solo 1 nudge; correcto (más = quemar turnos). Mantener. |
| `CONTEXT_SCOPE_HINT` ≥4 items → "split and STOP" | `server.py:619-638`, inyectado en `server.py:1689` | ¡Corta tareas largas legítimas de raíz! Una migración sobre 20 archivos NO es una lista de 20 tareas, pero el texto empuja al modelo a negarse/partir. P1: restringirlo a tareas con items INDEPENDIENTES, no archivos relacionados. |
| Countdown warning (`TURN_WARN_REMAINING=3`) | `server.py:156`, disparo `2026-2053` | Solo aviso… pero trae el commit (§6). Mantener el aviso, sanear el texto. |
| F2: no ejecutar tools del último turno | `server.py:1930-1940` | Diseño correcto (ahorra I/O muerto), pero hace que `hit_turn_limit` sea más caro: el agente "llega" a su última acción y no se ejecuta. |
| Dedup F3 + evicción | `server.py:1995-2004` vs `1735` | El deadlock descripto en §2 — el peor hallazgo secundario de esta auditoría. |

## 8. Diseño heartbeat/progreso + CLI

### 8.1 Archivo de progreso por dispatch (JSONL, append por turno)
- Path: `{CACHE_DIR}/progress/{dispatch_id}.jsonl` junto al dispatches.jsonl (`_LOG_PATH`, `server.py:827-830`); `dispatch_id` = ya existe como `dispatch_id` en el log final (agregar `uuid4().hex[:12]` al inicio de `_delegate_one_impl` si no está).
- Una línea por TURNO (append inmediato, flush + fsync barato), campos:
  ```json
  {"ts":"2026-09-24T14:03:12Z","dispatch_id":"a1b2c3","agent":"webdev","model":"glm-coding-plan-think","turn":17,"max_turns":60,"tool":"run_bash","tool_args_digest":"pytest -q (sha1:9f2c…)","last_bash_exit":0,"elapsed_s":412.7,"turn_s":18.3,"context_msgs":58,"evicted_total":4,"stop_reason":null}
  ```
  - `tool`: nombre de la tool del turno (o lista breve si son varias: `[read_file x3, run_bash]`).
  - `last_bash_exit`: ya se trackea como `last_bash_exit` en el loop (`server.py:1727`) — solo hay que emitirlo.
  - `evicted_total`: el acumulador `evicted_blocks` (`server.py:1733-1735`).
  - Línea FINAL con `"event":"done"` + `success`, `turns`, `tool_calls`, `hit_turn_limit` (los mismos campos que `_LOG_FIELDS`, `server.py:832-840`) para que el polling tenga closure sin leer dispatches.jsonl.
- Escritura desde `_delegate_one_impl`: (a) tras ejecutar las tools del turno (ahí se conocen tool/exit), (b) en cada return temprano (`1738`, `1760`, `1792`, timeouts) con `"event":"error"`. Wrap en try/except para que NUNCA tumbe un dispatch por un problema de logging.
- Orquestador: `tail -f` o polling `readlines()[-1]`; si `now - ts > 3*TURN_TYPICAL` sin línea nueva y sin `done` → probable hang del backend (distingue vivo/colgado, que hoy es imposible, §4).

### 8.2 CLI: `python server.py dispatch --agent webdev --model glm-coding-plan-think --task-file task.md --bg`
- Hoy NO existe ningún entrypoint CLI (`grep __main__/argparse` en server.py: nada; solo FastMCP stdio, `server.py:654`).
- Propuesta (`argparse`, stdlib, ~80 líneas):
  - `dispatch`: corre `_delegate_one_impl` en `asyncio.run` con `ctx=None` (la firma ya lo soporta: `server.py:1630-1633` "used when present"), imprime el JSON final a stdout y escribe progreso (§8.1). `--max-turns`, `--timeout`, `--follow` (stream del JSONL de progreso al stderr).
  - `dispatch --bg`: `subprocess.Popen` con `start_new_session=True` (nuevo process group, para que el Ctrl-C de la shell no lo mate), `nohup`-style; imprime `dispatch_id` + path del JSONL de progreso y sale.
  - `status <dispatch_id>`: lee la última línea del JSONL (turno actual, elapsed, última tool) o el JSON final si hay `done`.
- Esto da al usuario de shell lo mismo que tendría un subagente nativo: lanzar y olvidar, polling barato, JSON final parseable.

## 9. Lista priorizada de fixes con prueba que lo demuestra

**P0 — bloquean completar la tarea (los 3 fallos):**
1. **Eviction × dedup deadlock** (§2): limpiar `seen_calls` de lo desalojado. Test (`tests/test_context_pruning.py`, estilo del existente `evicted == 7`, línea 35-37): evictar un tool_result, re-emitir la MISMA llamada `read_file` y assert de que se RE-EJECUTA (no `deduped_calls += 1` ni mensaje "ya está en tu contexto").
2. **`HARD_MAX_TURNS` configurable + default cloud 60** (§1): `DELEGATE_HARD_MAX_TURNS=150`, `DELEGATE_CLOUD_MAX_TURNS` default 60, local queda 25. Test: con env seteada, `_delegate_one_impl(max_turns=90)` NO clampea a 40 (hoy `min(90,40)=40`, `server.py:1646`); y `model="local-…"` sin max_turns sigue en 25. Actualizar docstring `server.py:2247`.
3. **`RUN_BASH_TIMEOUT` por backend (cloud 600)** + arg `timeout` opcional en run_bash con clamp (`server.py:595`, `966-976`). Test: comando `sleep 130` con env 600 y alias cloud → exit 0; con default local → timeout + `_kill_process_group` (mock de proc para no dormir 130 s reales).
4. **Quitar el commit del countdown** (§6, `server.py:2050`): texto neutral "persiste con write_file y responde el estado". Test: agente de ESCRITURA cuyo countdown dispara → assert de que el tool_result NO contiene "git commit" (hoy lo contiene; complemento inverso del que motivó ebce68a).
5. **Fecha en el system prompt** (§5, insertar en `server.py:1674`). Test: assert `time.strftime('%Y-%m-%d')` in full_system para cualquier agente.

**P1 — degradan/dificultan (semanas):**
6. `DELEGATE_KEEP_TOOL_RESULTS=20` para cloud (`server.py:120` + selección por backend en `1735`) y marker con `path:líneas` (`1461-1464`). Test: KEEP=20 cloud evicta menos; el marker contiene el path del resultado desalojado.
7. Archivo de progreso por dispatch + línea `done` (§8.1). Test (`tests/test_dispatch_log.py` estilo): tras un dispatch mockeado de 3 turnos, el JSONL tiene 3 líneas de turno + 1 `done` con `success`; un return temprano por timeout deja línea `event:error`.
8. CLI `dispatch`/`dispatch --bg`/`status` (§8.2). Test: `--bg` devuelve inmediatamente un dispatch_id; `status` llega a `done` con el mismo JSON que imprime el modo foreground (subprocess real con backend mockeado).
9. Revisar `CONTEXT_SCOPE_HINT` (§7, `server.py:619-638`): no frenar tareas multi-ARCHIVO. Test: prompt con tarea de 20 archivos relacionados NO contiene "split into separate dispatches".
10. Truncado run_bash 12000/4000 → 20000/8000 cloud (`server.py:982-989`). Test de unidades: salida de 15K chars no se corta a 12K con flag cloud.

**P2 — robustez/observabilidad (cuando haya tiempo):**
11. `DELEGATE_DISPATCH_TIMEOUT=5400` cloud y `DELEGATE_DEFAULT_MAX_TOKENS` como env (`server.py:87`, `128`); re-evaluar `glm-coding-plan-think` a 131072 si aparecen `stop_reason=max_tokens`. Test de clamp/override existentes en `test_provider_pools.py`/`test_hardening.py` extendidos a las nuevas envs.
12. `report_progress` enriquecido (incluir `elapsed_s` y `last_bash_exit` en `server.py:1748-1753`) para clientes que sí renderizan notificaciones.

## Nota final
No encontré overlay `CLAUDE.md`/`AGENTS.md` del proyecto en el worktree que contradiga lo anterior; si el proyecto quiere fijar los defaults por backend (cloud vs local) como política, conviene plasmarlos en un overlay `.claude/agents/webdev.md` además del código.
