@echo off
echo === Uruchamianie CDA Downloader ===

if not exist venv (
    echo Nie znaleziono srodowiska wirtualnego. Uruchom najpierw install.bat
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

echo Aplikacja startuje na http://localhost:5000
echo (zatrzymanie: Ctrl+C)
echo.

python app.py

pause
