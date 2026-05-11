@echo off
REM Always run with Python 3.11 (see README).
set PY=py -3.11

echo Starting Homey Bot 2.0 - Space Lord Version
echo.

if exist "venviron\Scripts\activate.bat" (
  echo Activating virtual environment...
  call venviron\Scripts\activate.bat
  echo Installing requirements...
  python -m pip install -r requirements.txt
  echo Starting bot...
  python homey_bot_space_lord.py
) else if exist "venv\Scripts\activate.bat" (
  echo Activating virtual environment...
  call venv\Scripts\activate.bat
  echo Installing requirements...
  python -m pip install -r requirements.txt
  echo Starting bot...
  python homey_bot_space_lord.py
) else (
  echo No venv folder found - using Python 3.11 from the launcher.
  echo Tip: py -3.11 -m venv venv
  echo.
  echo Installing requirements...
  %PY% -m pip install -r requirements.txt
  echo Starting bot...
  %PY% homey_bot_space_lord.py
)
pause
