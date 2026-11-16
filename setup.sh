#!/usr/bin/env bash
# financial-hot-article setup (macOS / Linux): creates .venv inside this skill folder and installs dependencies.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "Python not found. Install Python 3.10+ first."; exit 1; fi

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  echo "[1/3] Creating .venv ..."
  "$PY" -m venv "$ROOT/.venv" || { echo "venv creation failed (on Debian/Ubuntu: apt install python3-venv)"; exit 1; }
else
  echo "[1/3] .venv already exists, reusing it."
fi
VPY="$ROOT/.venv/bin/python"

echo "[2/3] Installing dependencies ..."
"$VPY" -m pip install -U pip -q
if ! "$VPY" -m pip install -U -r "$ROOT/requirements.txt"; then
  echo "PyPI failed, retrying with Tsinghua mirror ..."
  "$VPY" -m pip install -U -r "$ROOT/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
fi

echo "[3/3] Self test ..."
PYTHONUTF8=1 "$VPY" "$ROOT/scripts/mkt.py" selftest
echo
echo "Done. Dependencies live in: $ROOT/.venv  (delete this folder to uninstall)"
