# Security review 2026-09-24 — fixes P0 de `AUDIT-2026-09-24-long-tasks.md` §9

Repo: `claude-delegate-local` (worktree de auditoría). Alcance: diff no commiteado (`git diff` de `server.py`, `tests/test_context_pruning.py`, `tests/test_turn_countdown.py`) + nuevo `tests/test_long_task_p0.py`. READ-ONLY: ningún archivo de código tocado; solo este informe.
Nota de proceso: `~/.claude/roles/security.md` no fue legible con la tool de lectura (path fuera del workdir); se obtuvo vía shell y se siguió. No existe overlay `.claude/agents/security.md` ni reports previos en este repo (`docs/`, `.claude/reports/` vacíos) — no hay veredictos anteriores que reconciliar; el fix previo relacionado es `ebce68a` (countdown × solo-lectura).

Verificación: `pytest tests/test_long_task_p0.py tests/test_context_pruning.py tests/test_turn_countdown.py` → **35 passed**. `git status` sin cambios adicionales.

## Cambios revisados (evidencia)
- `HARD_MAX_TURNS` configurable default 150 (`server.py:78`); default cloud `CLOUD_MAX_TURNS=60` (`server.py:74`), local 25 (`server.py:70`); clamp `server.py:1727-1728`; docstring tool `server.py:2350-2360`.
- `_is_local_backend` (`server.py:612-615`), `_default_bash_timeout` (`server.py:618-630`), `_resolve_bash_timeout` (`server.py:633-638`), `CLOUD_RUN_BASH_TIMEOUT=600` (`server.py:607`), `RUN_BASH_MAX=1800` (`server.py:609`).
- run_bash: schema con `timeout` (`server.py:786-800`), `_run_bash(..., timeout, default_timeout)` (`server.py:1007-1028`), paso por `_execute_tool` (`server.py:1061-1063`, `1143-1149`), default por dispatch `server.py:1823`.
- Eviction limpia dedup: `_evict_old_tool_results(..., seen_calls, call_key_by_id)` (`server.py:1500-1548`, pop en `1544-1547`); registro `call_key_by_id[tu_id]=call_key` en `server.py:2098-2100`.
- Countdown sin commit: `server.py:2141-2152` (texto nuevo); no queda ninguna instrucción de git en runtime (grep "commit/git add": solo comentarios `server.py:158-168`, `2134`).
- Fecha: `Today's date: {time.strftime('%Y-%m-%d')}` en el system prompt (`server.py:1758-1760`).

## (1) Clamp del `timeout` por llamada — 1 LOW

`_resolve_bash_timeout` (`server.py:633-638`) es sólido contra string (`"600"`→default), bool (`True`→default, chequeo explícito `isinstance(timeout, bool)`), negativo/0 (<1→default), float válido (`1e9`→1800, `1.5`→1) y enteros gigantes (`min` antes de `int`, sin overflow). Verificado empíricamente. **Ningún valor escapa del rango [1,1800] hacia un runtime más largo.**

- **LOW — NaN/Infinity filtra el proceso huérfano (demostrado).** `json.loads` stdlib (usado en `server.py:1354`, `1363`, `1427`) acepta los literales `NaN`/`Infinity`; un backend puede emitir `{"command":"sleep 5","timeout":NaN}`. En `_resolve_bash_timeout`, `nan < 1` es `False` → `int(nan)` lanza `ValueError` (`int(inf)` → `OverflowError`). Como `_resolve_bash_timeout` se llama DESPUÉS de `create_subprocess_shell` (`server.py:1015` vs `1024`), la excepción escapa de `_run_bash` (el `try` de `1027` no la cubre), la atrapa `_execute_tool` y devuelve `ERROR: ValueError...` — pero el hijo ya nació y **nadie lo mata ni lo espera**: sin timeout, sin `_kill_process_group`. Reproducido: pid `sleep 5` vivo tras el error; repetible cada turno → acumulación de huérfanos. Fix mínimo: calcular `eff_timeout` ANTES del spawn y envolver el `int()` en try/except (ValueError/OverflowError → default).
- Nota: la validación de schema (`minimum/maximum`, `server.py:790-795`) es documental — el loop no valida contra el schema; el clamp imperativo es la única barrera (y cumple).

## (2) `_is_local_backend` — fail-open — 1 LOW

`server.py:612-615`: solo `local-*` y `ornith*` son "local"; **cualquier alias nuevo/desconocido cae al lado cloud: 60 turnos y 600 s de bash** (`server.py:618-630`, `1727`). Es fail-open: los defaults conservadores (GPU/event-loop compartidos, semáforo `_BASH_MAX_CONCURRENCY=4`) se pierden por un typo o un alias local futuro (`omlx-*`, `mlx-*`). No hay path de atacante (el alias debe existir en el proxy LiteLLM para que el dispatch progrese; lo elige el orquestador/operador), por eso LOW y no MEDIUM. Fix: invertir el default (local salvo allowlist de cloud) o clasificar por config del provider, no por prefijo de string.

## (3) Turn ceiling × DISPATCH_TIMEOUT — acotado, no "horas" — OK (con nota)

El techo de turnos NO es la restricción vinculante: el deadline total se chequea al inicio de cada turno (`server.py:1830-1836`, con `DISPATCH_MIN_SLICE` en `1853-1860`) **y antes de CADA tool** (`server.py:2043-2049`, añadido preexistente que cubre el caso K-tools). El request por intento usa `timeout=min(TURN_TIMEOUT, remaining)` (`server.py:1871`). Peor caso con defaults: una tool que arranca en `deadline-1s` corre hasta su `eff_timeout` ≤ 1800 s → **un dispatch individual ≤ ~DISPATCH_TIMEOUT+1800 ≈ 90 min**; en batch, `BATCH_TASK_TIMEOUT=DISPATCH_TIMEOUT+900` (`server.py:643-650`) cancela antes vía `wait_for`. "Horas" solo si el operador sube `DELEGATE_DISPATCH_TIMEOUT`/`DELEGATE_RUN_BASH_MAX` (P2 del audit propone 5400 → peor caso ~2 h). Nota LOW de disponibilidad: con cloud default 600 s, un dispatch puede retener un slot del semáforo bash global ~4× más tiempo por llamada que antes (competencia entre dispatchos; el per-tool deadline check la acota a lo que reste del dispatch).

## (4) Eviction × dedup — re-ejecución de run_bash no idempotente — 1 MEDIUM

La corrección del deadlock es correcta y está bien testeada (`tests/test_context_pruning.py:262-355`: unidad + integración; `seen == {}` tras evicción, re-ejecución exactamente en el turno 8). Pero el dedup aplica a `read_file` **y `run_bash`** (`server.py:2088-2089`; solo `write_file` se excluye, `2083`), y el marker de desalojo invita genéricamente a "vuelve a pedirlo" (`server.py:1534-1535`). Consecuencia: el resultado de un `run_bash` con efectos NO idempotentes (`git reset --hard`, `dropdb`, `curl -X POST` a un deploy, publicar paquete) se desaloja → `seen_calls` se limpia (`1544-1547`) → si el modelo re-emite el comando idéntico (args byte-iguales, incl. `timeout`), **se re-ejecuta con efectos reales**. Antes de este diff el dedup lo suprimía (con mensaje falso). No cruza un boundary nuevo — run_bash ya es shell arbitrario por diseño y el modelo podría re-run con un arg distinto — pero el harness ahora *empuja* la re-ejecución justo cuando el contexto del modelo ya no muestra que ocurrió. MEDIUM (doble ejecución de efecto lateral no idempotente inducida por el propio harness). Fix mínimo: no limpiar `seen_calls` para claves de `run_bash` (solo `read_file`), o hacer el marker de run_bash explícito: "ya ejecutaste este comando; su salida se desalojó — no lo re-ejecutes salvo que deba repetirse por diseño". (`write_file`: excluido del dedup, sin cambio de comportamiento.)

Consistencia verificada además: re-evicción de un bloque ya marcado no ocurre (guard `body.startswith("[desalojado")`, `server.py:1531`); `call_key_by_id.pop` por `tool_use_id` limpia solo la entrada del bloque desalojado; re-ejecución posterior con nuevo `tu_id` re-registra (`2098-2100`) sin doble conteo.

## (5) Texto nuevo en system prompt / tool results — OK (2 notas LOW)

- Fecha (`server.py:1758-1760`): hecho estático, sin interpolación no confiable. OK.
- Countdown (`server.py:2145-2150`): sin git (verificado por grep y por `tests/test_turn_countdown.py:152-166`). *Nota LOW aceptada*: ordena a todo agente de escritura crear un archivo de estado en el workdir del orquestador — es exactamente el fix que el audit §6 propone y el test lo exige, pero es un side-effect (archivo extra sin tracker) que el orquestador no pidió explícitamente; si molesta, apuntarlo a un path dedicado (p.ej. `.delegate-state/` o el cache dir).
- Mensaje de timeout de run_bash (`server.py:1036-1039`): instrucción deliberada dentro del tool_result ("re-emitelo con timeout hasta 1800s") — palanca de disponibilidad acotada por el deadline (§3 arriba). Descripción de la tool (`server.py:786-800`): fáctica. Ningún texto nuevo interpola contenido de archivos ni de la tarea → sin canal de inyección indirecta nuevo.

## Resumen de severidades
| Sev | Hallazgo | Ref |
|---|---|---|
| MEDIUM | Re-ejecución de `run_bash` no idempotente tras evicción (marker invita + dedup limpiado) | `server.py:1534-1535`, `1544-1547`, `2088-2100` |
| LOW | NaN/Infinity en `timeout` → proceso huérfano sin timeout (clamp evaluado post-spawn) | `server.py:1015` vs `1024`, `633-638` |
| LOW | `_is_local_backend` fail-open: alias desconocido → defaults cloud (60 turnos/600 s) | `server.py:612-615` |
| LOW | Retención del semáforo bash global ×5 en cloud (600 s default por llamada) | `server.py:607`, `1013` |
| LOW | Aviso countdown induce escritura de archivo de estado en workdir ajeno (aceptado por diseño) | `server.py:2145-2150` |

Limpio/verificado sin hallazgos: clamp del arg `timeout` contra string/bool/negativo/gigantes (demostrado); acotación wall-clock del dispatch (deadline por turno y por tool); ausencia total de instrucciones git en runtime; fecha sin vector de inyección; integridad del guard de re-evicción y del mapa `call_key_by_id`; tests nuevos pasan (35/35) y no duermen tiempos reales (mocks de proc).

Ningún CRITICAL, ningún HIGH. Los MEDIUM/LOW no bloquean según política (próximo sprint). Recomendación: acompañar el merge con el fix LOW del NaN (mover `eff_timeout` antes del spawn) y decidir la política de re-ejecución de `run_bash` (MEDIUM) antes de despachar tareas con comandos no idempotentes.

VEREDICTO: APROBADO
