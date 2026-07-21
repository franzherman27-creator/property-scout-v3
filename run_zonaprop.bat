@echo off
setlocal

REM ── Cargar variables desde .env (ignora líneas vacías y comentarios con #) ──
for /f "usebackq eol=# tokens=1* delims==" %%A in ("%~dp0.env") do (
    if not "%%A"=="" set "%%A=%%B"
)

REM ── Variables específicas de esta tarea ──
set "ZONAS=la-plata"
set "HEADLESS=true"

REM ── Crear carpeta de log si no existe ──
if not exist "C:\property-scout\" mkdir "C:\property-scout\"

REM ── Ejecutar scraper ──
cd /d "%~dp0"
echo [%DATE% %TIME%] Iniciando run_zonaprop >> C:\property-scout\scraper.log 2>&1
python zonaprop_local.py >> C:\property-scout\scraper.log 2>&1
echo [%DATE% %TIME%] Fin run_zonaprop (exitcode=%ERRORLEVEL%) >> C:\property-scout\scraper.log 2>&1

endlocal
