@echo off
setlocal EnableExtensions
title Mark XXXIX
cd /d "%~dp0"

REM Prefer conda env "mark-coqui" (Coqui / CUDA stack) when conda is available.
set "CONDA_BAT="
if defined CONDA_EXE for %%I in ("%CONDA_EXE%") do (
  if exist "%%~dpI..\condabin\conda.bat" set "CONDA_BAT=%%~dpI..\condabin\conda.bat"
)
if not defined CONDA_BAT if exist "%LocalAppData%\miniconda3\condabin\conda.bat" (
  set "CONDA_BAT=%LocalAppData%\miniconda3\condabin\conda.bat"
)
if not defined CONDA_BAT if exist "%UserProfile%\miniconda3\condabin\conda.bat" (
  set "CONDA_BAT=%UserProfile%\miniconda3\condabin\conda.bat"
)
if not defined CONDA_BAT if exist "C:\ProgramData\miniconda3\condabin\conda.bat" (
  set "CONDA_BAT=C:\ProgramData\miniconda3\condabin\conda.bat"
)

if defined CONDA_BAT (
  echo [%TIME%] Starting Mark with conda env: mark-coqui
  "%CONDA_BAT%" run -n mark-coqui python "%~dp0main.py"
) else (
  echo [%TIME%] CONDA not found in usual locations — using "python" on PATH.
  python "%~dp0main.py"
)

if errorlevel 1 (
  echo.
  echo Mark exited with code %ERRORLEVEL%. See messages above.
  pause
)
