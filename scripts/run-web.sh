#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
exec "${AVIATION_PYTHON:-python}" -m uvicorn aviation_ft.web_app.server:app --app-dir "$project_dir/src" --host 127.0.0.1 --port "${AVIATION_PORT:-8501}"
