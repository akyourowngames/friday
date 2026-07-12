@echo off
echo === Building ares-server with Nuitka ===
echo.

REM Install Nuitka and compression deps if missing
pip install nuitka ordered-set zstandard
echo.

REM Navigate to repo root
cd /d "%~dp0"

REM Clean previous build
if exist "dist\ares-server" rmdir /s /q "dist\ares-server"
if exist "dist\ares-server.build" rmdir /s /q "dist\ares-server.build"

REM Build with Nuitka
python -m nuitka ^
  --standalone ^
  --windows-console-mode=disable ^
  --include-package=ares ^
  --include-package=ares.tools ^
  --include-package=ares.voice ^
  --include-package=ares.cron ^
  --include-package=ares.channels ^
  --include-package=ares.cli ^
  --include-package=ares.autonomy ^
  --include-package=rich ^
  --include-package=mcp ^
  --include-package=httpx ^
  --include-package=pydantic ^
  --include-package=sentence_transformers ^
  --include-package=transformers ^
  --nofollow-import-to=transformers ^
  --include-package=sqlite_vec ^
  --include-package=websockets ^
  --include-package=PIL ^
  --include-package=ddgs ^
  --include-package=faster_whisper ^
  --include-package=croniter ^
  --include-package=numpy ^
  --include-package=dateparser ^
  --include-data-dir=ares/skills=ares/skills ^
  --output-dir=dist ^
  --output-filename=ares-server ^
  ares/__main__.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo === BUILD FAILED ===
    exit /b 1
)

echo.
echo === Build successful! ===
echo Output: dist\ares-server\ares-server.exe
echo.
echo To test: dist\ares-server\ares-server.exe --host 127.0.0.1 --port 8765
echo.
