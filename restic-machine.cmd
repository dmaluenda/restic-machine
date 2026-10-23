@echo off
rem SPDX-License-Identifier: AGPL-3.0-or-later
rem Start restic-machine from this folder and open it in the browser (double-click me).
rem Closing this window stops it.
title restic-machine
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
where py >nul 2>nul
if %errorlevel%==0 (set "PYTHON=py -3") else (set "PYTHON=python")
%PYTHON% -m restic_machine serve %*
if errorlevel 1 pause
