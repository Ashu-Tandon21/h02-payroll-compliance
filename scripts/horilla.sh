#!/usr/bin/env bash
# Install and run Horilla locally (no Docker), with Brickline's made-up data entered.
#   ./scripts/horilla.sh setup        clone Horilla into var/horilla, install, create admin/admin, enter data as of June
#   ./scripts/horilla.sh start        run Horilla on http://localhost:8000
#   ./scripts/horilla.sh stage july   HR enters Priya's raise (as happened in July)
#   ./scripts/horilla.sh stop
# Tested with Horilla master of Nov 2026 on Python 3.12 (SQLite, no Redis).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
H="$ROOT/var/horilla"
PY="${PYTHON:-python3.12}"
export DEBUG=1 SECRET_KEY="${HORILLA_SECRET_KEY:-local-demo-only}" ALLOWED_HOSTS=localhost,127.0.0.1 \
       CSRF_TRUSTED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
unset DJANGO_SETTINGS_MODULE
export BRICKLINE_SEED="$ROOT/data/brickline/seed.json"  # the same people as the spreadsheet and the Demo HRMS
manage() { (cd "$H/src" && "$H/venv/bin/python" manage.py "$@"); }

case "${1:-}" in
  setup)
    mkdir -p "$H"
    [ -d "$H/src" ] || git clone --depth 1 https://github.com/horilla-opensource/horilla.git "$H/src"
    [ -x "$H/venv/bin/python" ] || "$PY" -m venv "$H/venv"
    "$H/venv/bin/pip" install -q --upgrade pip
    "$H/venv/bin/pip" install -q -r "$H/src/requirements.txt"
    manage migrate --noinput
    DJANGO_SUPERUSER_PASSWORD=admin manage createsuperuser --noinput --username admin --email admin@brickline.example 2>/dev/null || true
    BRICKLINE_STAGE=june manage shell < "$ROOT/integrations/horilla/seed_brickline.py"
    echo "Horilla is ready. Start it with: ./scripts/horilla.sh start   (login admin / admin)"
    ;;
  start)
    mkdir -p "$ROOT/var/logs" "$ROOT/var/pids"
    (cd "$H/src" && nohup "$H/venv/bin/python" manage.py runserver 127.0.0.1:8000 --noreload >> "$ROOT/var/logs/horilla.log" 2>&1 & echo $! > "$ROOT/var/pids/horilla.pid")
    echo "Horilla starting on http://localhost:8000 (admin / admin); log: var/logs/horilla.log"
    ;;
  stage)
    BRICKLINE_STAGE="${2:-july}" manage shell < "$ROOT/integrations/horilla/seed_brickline.py"
    ;;
  stop)
    [ -f "$ROOT/var/pids/horilla.pid" ] && kill "$(cat "$ROOT/var/pids/horilla.pid")" 2>/dev/null && rm -f "$ROOT/var/pids/horilla.pid" && echo "stopped Horilla" || true
    ;;
  *) echo "usage: $0 setup | start | stage june|july | stop"; exit 1 ;;
esac
