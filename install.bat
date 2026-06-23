@echo off
echo === Instalacja CDA Downloader ===

where python >nul 2>nul
if errorlevel 1 (
    echo Nie znaleziono Pythona w PATH. Zainstaluj Python 3.10+ i sprobuj ponownie.
    pause
    exit /b 1
)

if not exist venv (
    echo Tworze srodowisko wirtualne...
    python -m venv venv
)

echo Aktywuje srodowisko wirtualne...
call venv\Scripts\activate.bat

echo Instaluje zaleznosci...
pip install --upgrade pip
pip install -r requirements.txt

echo Instaluje przegladarke Chromium dla Playwright (moze potrwac kilka minut)...
playwright install chromium

echo.
echo === Instalacja zakonczona ===
echo Aby uruchomic aplikacje, uzyj pliku run.bat
pause
