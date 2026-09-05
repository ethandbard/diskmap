@echo off
rem Double-click launcher for the diskmap GUI.
rem pyw.exe runs it without a console window; py.exe is the fallback.
where pyw >nul 2>&1 && (
  start "" pyw "%~dp0diskmap.py" %*
) || (
  start "" py "%~dp0diskmap.py" %*
)
