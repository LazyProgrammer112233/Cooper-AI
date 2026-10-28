@echo off
REM Starts Cooper AI on http://localhost:8000 (Windows).
cd /d "%~dp0"
if not exist .venv ( py -3 -m venv .venv )
call .venv\Scripts\activate.bat
pip install -q -r backend\requirements.txt
if not exist frontend\dist (
  pushd frontend
  call npm install
  call npm run build
  popd
)
if not exist .env copy .env.example .env
echo Cooper AI: open http://localhost:8000
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
