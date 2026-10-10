#!/bin/bash
# reload-delegate.sh: reload_argv for neola-bifrost scripts/acceptance_t16.py (DLC-05).
#
# HONEST LIMIT. The delegate MCP is spawned by Claude Code once PER SESSION and reads
# DELEGATE_GATEWAY_FILE only at startup. This script CANNOT restart the MCP of any open
# session, and it does not kill or signal any process. A "reload" in practice means:
# the next session (or a restarted Claude Code) picks up the selector file's value.
#
# What it does, and all the judge requires of it (exit 0, well under the 300 s limit):
#   1. Reads the REAL selector file (path from $DELEGATE_GATEWAY_FILE) with the exact
#      same strict checks the MCP applies at startup (regular file, no symlink, own uid,
#      mode 0600, JSON {"selector": litellm|bifrost}). Any defect -> exit 1, so a bad
#      file is caught here and not by the first session that starts.
#   2. Prints which gateway a freshly spawned MCP would use.
# It never prints file contents or key values. The drill edits only a COPY of the
# selector; this script validates the real one, so a green DLC-05 does not prove a
# running session re-read the file (the runbook says the first canary session does).
set -euo pipefail
: "${DELEGATE_GATEWAY_FILE:?set DELEGATE_GATEWAY_FILE to the real selector path}"
here="$(cd "$(dirname "$0")/.." && pwd)"
# Import server.py in a throwaway interpreter: it runs the same startup validation.
# DELEGATE_BIFROST_URL is only required for the bifrost value; pass through if set.
exec uv run --quiet --directory "$here" python - "$here" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("server_reload_check", sys.argv[1] + "/server.py")
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except ValueError as e:
    print("selector invalid: %s" % e, file=sys.stderr)
    sys.exit(1)
print("next MCP spawn will use gateway: %s" % m.GATEWAY)
PY
