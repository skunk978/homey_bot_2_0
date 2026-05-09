@echo off
echo Starting Homey Bot 2.0 - Space Lord Version
echo.
echo Make sure you have Python 3.11 and the required packages installed
echo.
echo Activating virtual environment...
call venviron\Scripts\activate.bat
echo.
echo Installing requirements...
pip install -r requirements.txt
echo.
echo Starting bot...
python homey_bot_space_lord.py
pause
