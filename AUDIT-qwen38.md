# Auditoría de rendimiento — claude-delegate-local (server.py, 2367 líneas)

Auditor: agente `coder` despachado por el propio harness (perspectiva privilegiada: soy
uno de los modelos que este server maneja). Lectura completa de `server.py` el 2026-08-18.

**Alcance pedido:** por qué un despacho genera 8000–38000 tokens de salida, trabajo
desperdiciado, calidad del feedback de errores de herramientas, y todo lo que me hace
trabajar peor. **No se repite** el hallazgo ya conocido de `MAX_COMPLETION_NUDGES`
(server.py:106).

**Linea base aceptada (no se re-analiza):** mediana 3s / p90 30s / p99 124s sobre 8196
requests GLM-5.3; las lentas no están colgadas (73 tok/s sostenido); 0 requests sin
terminar; cache hit 77.4%.

---

## 0. Lo que YA está bien resuelto (no tocar)

Verificado en el código; estas piezas son correctas y explican la linea base sana:

- **Cache de prompt con breakpoints explícitos y copia antes de mutar** —
  `_apply_cache_control` (server.py:1194-1247). El kill-switch (server.py:110), la
  allowlist de proveedores (server.py:1179-1191) y la copia de `tools`/`messages` para
  no contaminar `AGENT_TOOLS` ni el historial compartido (server.py:1215-1224) son la
  razón del 77.4% de cache hit. Correcto.
- **Reintentos clasificados y sin multiplicación** — `RETRYABLE_STATUS` con el 529
  (server.py:350-353), fail-fast en 4xx (server.py:1452-1456), backoff con jitter y
  respeto de `Retry-After` con techo (server.py:381-412), y `x-litellm-num-retries: 0`
  para que el proxy no reintente por su cuenta (server.py:1277-1285). Correcto.
- **Deadline global que cubre todo** — turnos (server.py:1408), cada intento backend
  acotado al resto del deadline (server.py:1443-1450), ejecución de herramientas
  (server.py:1563-1569), chequeo `DISPATCH_MIN_SLICE` antes de arrancar otro intento
  (server.py:1431-1442) y backoff que no duerme más de lo que queda (server.py:1461-1474).
- **Concurrencia en dos capas** — semáforo asyncio por proveedor (server.py:161-223,
  1662-1667) + slots cross-process con `flock` que el kernel libera si el proceso muere
  (server.py:674-720). El bug histórico de 4 instancias × 6 slots = 24 está cerrado.
- **Presupuesto de max_tokens por alias, explícito y clampeado al cap del proveedor** —
  `MODEL_BUDGET_POLICY` (server.py:245-255), `_resolve_max_tokens` (server.py:308-339),
  caps GLM/qwen (server.py:139-146). Correcto.
- **El nudge ya está bien acotado**: solo interroga turnos terminados normalmente con
  texto (`_should_nudge`, server.py:274-285); un turno cortado por `max_tokens` o vacío
  falla rápido en vez de re-preguntar (server.py:1535-1548), y `resumed_after_nudge`
  queda expuesto para desconfiar de esas corridas (server.py:1550-1554, 1612-1617).
- **Streaming + cliente HTTP compartido** (server.py:999, 1008-1017), **marcadores de
  truncado en stdout de bash** (server.py:763-770), **paginación de read_file con
  instrucción de continuación** (server.py:813-817), **allowlist de entorno para
  subprocesos** (server.py:631-648), **clasificación de `incomplete`** que impide
  reportar éxito en corridas cortadas (server.py:1588-1599).

---

## 1. Hallazgos, ordenados por impacto real

### F1 — ALTO. El historial crece sin poda NUNCA: resultados de tools y reasoning completo de todos los turnos se reenvían en cada request

**Dónde:** server.py:1556 (assistant completo, con bloques `thinking`, se appendea),
server.py:1584 (tool_results se appendean íntegros), server.py:907-908 y 921-923
(`reasoning_content` se reenvía en la conversión a formato OpenAI).

**El problema.** `messages` solo crece durante todo el despacho; no existe ninguna poda,
compactación ni desalojo. Cada turno reenvía:
1. **Todos los tool_results históricos**, hasta 50000 chars por `read_file`
   (server.py:429 ≈ 12–13K tokens por lectura) y hasta 16000 chars por `run_bash`
   (server.py:763, ≈ 4K tokens).
2. **Todos los bloques `thinking`/`reasoning_content` históricos** del modelo. En modelos
   thinking (glm-coding-plan-max, qwen-3-8-max-think, deepseek-v4-flash) eso son
   5–30K tokens POR TURNO que vuelven a viajar como entrada en cada request posterior.

Un despacho de 20 turnos con 5 lecturas grandes y 3 bash verbosos llega al turno 20 con
200–400K tokens de historial. El propio código ya midió la consecuencia: el comentario de
server.py:170-176 documenta **241 requests de qwen con 10.5M tokens de ENTRADA contra
247K de SALIDA (ratio 42:1)** — el gasto lo domina el contexto rearrastrado, exactamente
este hallazgo.

**Agravante por formato.** Para backends en formato OpenAI (`deepseek-`, `qwen-`,
`minimax-`, `grok-`…, server.py:851-870) no se aplica ningún `cache_control`
(`_supports_prompt_caching` solo cubre Anthropic-format, server.py:1179-1191), así que
ese historial creciente se reprocesa entero cada turno. Y el `reasoning_content`
reenviado es en general **inutilizable** por el provider: el propio comentario de
server.py:951-952 admite que se preserva "para que el loop lo reincluya", pero DeepSeek
documenta que el reasoning histórico no se reaprovecha y solo se cobra como entrada.

**Por qué importa para la latencia medida:** el TTFT de cada turno crece con el historial;
en backends locales además se acerca al techo de contexto del slot (el mismo
`CONTEXT_SCOPE_HINT`, server.py:462-465, admite el riesgo) — y un contexto saturado
degrada la calidad y empuja al modelo a releer y repetir, retroalimentando F3.

**Cambio propuesto (acotado, opt-in por env):**
- **(a)** `_anthropic_to_openai_request`: no reenviar `reasoning_content` de turnos ya
  completados (guardar solo si el provider lo exige; detrás de
  `DELEGATE_RESEND_REASONING`, default 0). Para el path Anthropic, desalojar los bloques
  `thinking` de mensajes assistant que no sean el último (Anthropic permite descartar
  thinking de turnos terminados; el `signature` solo importa dentro del intercambio de
  tool-use en curso).
- **(b)** Desalojo de tool_results viejos: antes de armar el request del turno N, todo
  `tool_result` anterior a los últimos `DELEGATE_KEEP_TOOL_RESULTS` mensajes de tool
  (default sugerido: 6) se reemplaza por una línea
  `[resultado anterior desalojado para ahorrar contexto: read_file X líneas N-M]`
  (el modelo ya lo procesó; si lo necesita puede releer con offset/limit).

**Verificación:** el resultado ya expone `tokens_in`, `cache_read_tokens`, `elapsed_s` y
`turns` (server.py:1619-1625). Despachar la MISMA tarea multi-archivo antes y después y
comparar `tokens_in` por turno. **ANTES:** historial de 200–400K tokens al turno 20 en
despachos largos; `tokens_in` acumulado creciendo casi lineal por turno. **DESPUÉS:**
historial estable en ~30–80K tokens; en formato OpenAI sin caché implícita el TTFT de los
últimos turnos debería caer 3–8×. Para (a) además: 1 despacho A/B comparando que la tarea
se resuelve igual (el reasoning histórico no debería cambiar el resultado).

---

### F2 — ALTO. En el último turno permitido, las tool calls SE EJECUTAN aunque el modelo jamás verá el resultado

**Dónde:** condición del loop server.py:1407 (`while turn < max_turns`) + ejecución de
tools server.py:1556-1584 + `hit_turn_limit` server.py:1591.

**El problema.** Cuando `turn == max_turns` y la respuesta trae `tool_use`, el loop
igualmente ejecuta las herramientas (hasta K llamadas, cada `run_bash` con su timeout de
120s, server.py:442), appendea los resultados a `messages` (server.py:1584) y ahí el
`while` termina: **esos resultados nunca llegan al modelo**. Es trabajo 100% descartado:
I/O, CPU, hasta cientos de segundos de wall-clock, y retención del semáforo de bash
(server.py:738) que bloquea a otros despachos. El resultado encima sale
`incomplete=True` (server.py:1591-1593), así que el orquestador típicamente redespacha.

**Cambio propuesto:** si `turn >= max_turns` y hay `tool_uses`, no ejecutarlas; salir
directamente con `error` tipo `"hit turn limit still wanting to run: <nombres>"` (los
nombres ya están parseados en server.py:1571). 3 líneas dentro del loop.

**Verificación:** test unitario con backend fake que devuelve `tool_use` cuando
`max_turns=1`; assert de que `_execute_tool` no se invoca y el resultado trae el mensaje
nuevo. **ANTES:** 1–4 ejecuciones desperdiciadas + hasta ~120s×K de wall-clock por cada
despacho que toca el límite. **DESPUÉS:** salida inmediata con diagnóstico accionable
(le dice al orquestador subir `max_turns` o achicar la tarea).

---

### F3 — MEDIO-ALTO. Las llamadas a tools idénticas se re-ejecutan sin aviso (re-lecturas y re-greps pagan de nuevo)

**Dónde:** server.py:1557-1584 — el loop de ejecución no lleva ningún registro de
llamadas previas.

**El problema.** El modo de fallo clásico que el mismo `CONTEXT_SCOPE_HINT` intenta
prevenir (server.py:476-481, "never re-read a range you already saw") sigue ocurriendo:
el modelo, sobre todo con contexto saturado (ver F1), re-pide el mismo `read_file` o el
mismo `grep`. El harness lo ejecuta de nuevo, y el resultado idéntico vuelve a entrar al
historial — doble desperdicio: la ejecución y el re-arrastre de tokens.

**Cambio propuesto:** un dict `{(nombre, json.dumps(args, sort_keys=True)): turno}` por
despacho. Si la misma llamada se repite, devolver en el tool_result:
`"Llamada idéntica a la del turno N — el resultado ya está en tu contexto; si necesitas
otra cosa cambia offset/limit o el comando."` (sin re-ejecutar; opcionalmente adjuntar el
resultado cacheado si es corto). Excepción deliberada: NO deduplicar `write_file` (re-escrituras
legítimas) y deduplicar `run_bash` solo si el comando es exactamente idéntico.

**Verificación:** contador `deduped_calls` en el resultado del despacho (misma forma que
`malformed_calls`, server.py:1611). **ANTES:** cada re-lectura cuesta I/O + hasta ~13K
tokens re-arrastrados por el resto del despacho. **DESPUÉS:** el loop de truncado/re-lectura
se corta en 1 turno con una señal correctiva explícita; en despachos largos típicos espero
1–3 llamadas deduplicadas.

---

### F4 — MEDIO. Cuando una tool falla, el agente recibe feedback insuficiente y gasta turnos a ciegas

**Dónde y ejemplos concretos:**
- **Tool malformada:** server.py:1576 devuelve
  `"ERROR: tool inválida o args mal formados ({name=})"` — no dice QUÉ argumento falta o
  sobra ni muestra el schema esperado. Un modelo flojo de tool-calling prueba variantes a
  ciegas.
- **JSON de la tool call truncado:** si el stream se corta a medio `input_json_delta`,
  el input se parsea a `{}` (server.py:1106-1108); la llamada sigue con args vacíos y el
  modelo recibe `ERROR: KeyError: 'path'` (vía server.py:798→843) sin pista de que SU
  llamada llegó truncada. La va a repetir igual.
- **Archivo inexistente:** `open()` en server.py:794 lanza `FileNotFoundError`, capturado
  genéricamente en server.py:842-843: `ERROR: FileNotFoundError: [Errno 2] ...`. El modelo
  sabe el path que falló pero no qué HAY en el directorio — gasta 1–2 turnos adivinando
  nombres cuando un `ls` del directorio padre en el mensaje lo resolvía.
- **Timeout de bash:** `ERROR: command timeout (120s)` (server.py:757) sin sugerir
  acotarlo (grep/head/timeout interno).

**Qué sí está bien:** errores de path-sandbox con el path citado (server.py:609),
exit_code + stdout/stderr + marcador de truncado en bash (server.py:763-775), y el hint de
continuación de read_file (server.py:813-817).

**Cambio propuesto (mensajes, no lógica):**
1. server.py:1576: incluir qué falló exactamente (`args` recibidos + params requeridos del
   schema de la tool, que ya está en `AGENT_TOOLS`).
2. server.py:1106-1108: marcar el tool_use con un flag `"_input_truncated"` y, en el loop,
   responder `"Tu llamada llegó truncada (JSON incompleto); re-emítela completa"` en vez de
   ejecutar con `{}`.
3. `FileNotFoundError` en read_file: capturarla específicamente y añadir al mensaje las
   primeras ~20 entradas del directorio padre (`os.listdir`) + "usa run_bash ls/find para
   ubicarlo".
4. Timeout de bash: añadir "re-ejecuta con una versión acotada (grep/head/-m 1)".

**Verificación:** despachar una tarea con un path mal escrito a propósito y contar turnos
hasta recuperarse. **ANTES:** 1–3 turnos ciegos por fallo (cada uno con su request
completo al backend). **DESPUÉS:** corrección en 1 turno en la mayoría de los casos.

---

### F5 — MEDIO. El sistema empuja respuestas largas: presupuesto enorme por turno sin ninguna contrapresión de brevedad

**Dónde:** presupuestos server.py:118-119 (65536 default, 150000 max-tier) y 245-255
(131072 para glm/qwen); system prompt server.py:1382-1390.

**Respuesta directa a la pregunta 1 de esta auditoría (por qué 8000–38000 tokens de
salida):**
- El system prompt del harness (server.py:1382-1390) y el `CONTEXT_SCOPE_HINT`
  (server.py:466-485) controlan bien la ENTRADA (no cargar todo de golpe, leer por rangos,
  sintetizar temprano) pero **no dicen nada sobre la longitud de la SALIDA**. La única
  instrucción de cierre es "respond with a final text message WITHOUT tool_use"
  (server.py:1387) — sin criterio de tamaño ni de "qué cuenta como terminado".
- Con 65–150K tokens de presupuesto por turno, un modelo thinking llena el espacio si la
  tarea es abierta. A 73 tok/s medidos, 8000 tokens de salida ≈ 110s y 38000 ≈ 520s —
  eso ES el p90/p99 de la linea base. Las lentas no están colgadas: están escribiendo.
- El cuerpo del agente se appendea crudo (server.py:1389); el harness no lo compensa.
  (El .md de `coder`, ~1.8KB, es breve; los agentes de análisis/auditoría son el riesgo.)

**Cambio propuesto:** añadir al system prompt (server.py:1382-1390), tras la línea del
cierre:
> "Keep every intermediate message short (1-3 lines). Put large artifacts in FILES via
> write_file — never paste long content into a response. Your final answer should be a
> compact summary (< ~200 palabras) pointing at what you wrote/verified, unless the task
> explicitly asks for inline long-form text."

Además evalué y **descarto** tocar el bloque `thinking` del request (Anthropic
`thinking.budget_tokens`): la config de thinking vive en los alias de LiteLLM
(server.py:860-869 lo documenta) y un parámetro del harness chocaría con ella. La palanca
correcta para el razonamiento es el presupuesto del alias, ya resuelto.

**Verificación:** A/B de la misma tarea de auditoría/escritura antes y después, comparando
`tokens_out` y `elapsed_s` del resultado. **ANTES:** finales de 8–38K tokens en tareas
abiertas. **DESPUÉS:** espero que la cola larga se comprima cuando el deliverable va a
archivo; métrica de éxito: p90 de `tokens_out` cayendo >50% en tareas de informe. (No
afecta a los tokens de thinking — esos los controla el alias.)

---

### F6 — MEDIO-BAJO. El agente no ve su presupuesto de turnos

**Dónde:** server.py:1382-1390. `CONTEXT_SCOPE_HINT` dice "limited turn budget"
(server.py:482) sin número; `report_progress` (server.py:1420-1425) va al ORQUESTADOR, no
al agente.

**El problema.** Yo (como modelo despachado) no sé si tengo 5 o 25 turnos. Con el default
de 25 (server.py:65-73) no tengo incentivo a sintetizar temprano; un despacho de 10 turnos
bien podía cerrarse en 6 si el modelo supiera el techo. Es el mismo principio que el
harness ya aplica a la lectura ("SYNTHESIZE EARLY", server.py:482) pero sin dar el dato.

**Cambio propuesto (1 línea):** en `full_system` (server.py:1382-1390):
`f"Turn budget: {max_turns} tool-calling turns (hard stop). Plan to finish with margin.\n"`.
Opcional fase 2: cuando quede ≤20% del presupuesto, añadir una línea al siguiente
tool_result. La fase 1 es la acotada.

**Verificación:** A/B midiendo distribución de `turns` en despachos equivalentes.
**ANTES:** uso del presupuesto completo por defecto de inercia. **DESPUÉS:** leve baja de
turnos promedio; costo cero (tokens despreciables, cacheado en el system).

---

### F7 — BAJO. `delegate_batch` docstring miente sobre el límite (dice default 2, es 12)

**Dónde:** server.py:1788 ("Hard cap MAX_BATCH_SIZE (default 2)") vs server.py:1755
(`MAX_BATCH_SIZE = …"12"`).

**El problema.** El orquestador (Claude Code) lee el docstring para decidir cómo lotear:
si cree que el tope es 2, troza un lote de 6 en 3 llamadas secuenciales, perdiendo el
paralelismo y pagando 3 round-trips MCP en vez de 1. El cuerpo del docstring incluso se
contradice a sí mismo (server.py:1773 dice "batch cap = MAX_BATCH_SIZE, default 12").

**Cambio propuesto:** corregir server.py:1788 a "(default 12)".

**Verificación:** grep. **ANTES:** lotes artificialmente chicos cuando el orquestador
confía en el docstring. **DESPUÉS:** lotes del tamaño correcto a la primera.

---

### F8 — BAJO. `delegate_to_provider` hardcodea `max_turns=25` y se saltea el sentinel AUTO

**Dónde:** server.py:2334 (`max_turns: int = DEFAULT_MAX_TURNS`) vs
server.py:1693 (`max_turns: int = 0`) y la resolución AUTO server.py:1353-1356.

**El problema.** Con default 25 explícito, la rama AUTO (`LOCAL_MAX_TURNS` vs
`CLOUD_MAX_TURNS`) nunca aplica por esta ruta: si alguien ajusta
`DELEGATE_CLOUD_MAX_TURNS`, `delegate_to_provider` lo ignora en silencio. Hoy ambos valen
25 así que no hay diferencia observable — es una trampa futura, no un bug activo.

**Cambio propuesto:** default `max_turns: int = 0` en la firma de
`delegate_to_provider` (server.py:2334).

**Verificación:** diff + 1 despacho por esa ruta sin max_turns verificando que el
resultado trae `max_turns` resuelto por modelo (campo ya existente, server.py:1609).

---

### F9 — NOTA (sin cambio). Costo de reintentar un turno largo que murió al final

Cuando un stream se corta al 95% de una generación de 30K tokens (429/529 mid-stream,
`BackendStreamError` retryable, server.py:1481-1489), el reintento regenera TODO el turno
desde cero. Es inherente al protocolo (no hay resume) y la política actual es la correcta;
la mitigación real es reducir la generación por turno (F5) y el historial (F1), no tocar
los reintentos. Se registra para que no se "arregle" en una futura revisión.

---

## 2. Resumen de prioridades

| # | Hallazgo | Impacto | Esfuerzo |
|---|----------|---------|----------|
| F1 | Historial sin poda + reasoning reenviado | ALTO (domina el gasto: ratio 42:1 medido) | medio, opt-in por env |
| F2 | Tools ejecutadas en el turno final sin consumidor | ALTO (wall-clock puro tirado) | ~3 líneas |
| F3 | Sin dedup de llamadas idénticas | MEDIO-ALTO | bajo |
| F4 | Feedback pobre en errores de tools | MEDIO (turnos ciegos) | bajo (mensajes) |
| F5 | Sin contrapresión de brevedad; presupuesto 65–150K/turno | MEDIO (cola p90/p99) | 3 líneas de prompt |
| F6 | Turn budget invisible para el agente | MEDIO-BAJO | 1 línea |
| F7 | Docstring de batch dice 2, es 12 | BAJO | 1 palabra |
| F8 | provider-tool ignora el sentinel AUTO | BAJO (trampa futura) | 1 carácter |

**Recomendación de orden de implementación:** F2 y F7 (baratos, sin riesgo), luego F5+F6
(solo prompt, medibles por A/B), luego F4 (mensajes), y por último F1 y F3 que tocan el
payload al backend y requieren la verificación A/B con `tokens_in` descrita arriba.

*Fin del informe — generado por el agente coder despachado vía el propio harness.*
