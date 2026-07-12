#!/bin/bash
set -e

echo "=== Building ares-server with Nuitka ==="
echo

# Install Nuitka and compression deps if missing
pip install nuitka ordered-set zstandard
echo

# Navigate to repo root
cd "$(dirname "$0")"

# Clean previous build
rm -rf dist/ares-server dist/ares-server.build

# Build with Nuitka
python -m nuitka \
  --standalone \
  --include-package=ares \
  --include-package=ares.tools \
  --include-package=ares.voice \
  --include-package=ares.cron \
  --include-package=ares.channels \
  --include-package=ares.cli \
  --include-package=ares.autonomy \
  --include-package=rich \
  --include-package=mcp \
  --include-package=httpx \
  --include-package=pydantic \
  --include-package=sentence_transformers \
  --include-package=transformers \
  --nofollow-import-to=transformers \
  --include-package=sqlite_vec \
  --include-package=websockets \
  --include-package=PIL \
  --include-package=ddgs \
  --include-package=faster_whisper \
  --include-package=croniter \
  --include-package=numpy \
  --include-package=dateparser \
  --include-data-dir=ares/skills=ares/skills \
  --output-dir=dist \
  --output-filename=ares-server \
  ares/__main__.py

echo
echo "=== Build successful! ==="
echo "Output: dist/ares-server/ares-server"
echo
echo "To test: dist/ares-server/ares-server --host 127.0.0.1 --port 8765"
