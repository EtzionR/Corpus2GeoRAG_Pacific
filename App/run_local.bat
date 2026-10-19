@echo off
rem Runs the App locally. With ..\Agent\.env (OPENROUTER_API_KEY) it uses the real agent;
rem without it, the agent's recorded mock replies. Extra arguments go to streamlit run.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo The virtual environment is missing. Run setup_local.bat first.
    exit /b 1
)

rem The model provider sometimes stalls a request until the agent's 60 s retry, so a few
rem answers take 70-90 s; 240 s per question avoids cutting those off (the agent's default is 120).
if not defined GEO_AGENT_RUN_TIMEOUT set GEO_AGENT_RUN_TIMEOUT=240

rem Listen on this computer only: with a real key, anyone who can reach the App can spend it.
".venv\Scripts\python.exe" -m streamlit run app.py --server.address localhost %*
