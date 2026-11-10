@echo off
title HM Algo 2.0 Trading Terminal
cd /d "%~dp0"
set "JARVIS_CONFIRM_LIVE=1"
echo ======================================================================
echo           Starting HM Algo 2.0 Live Trading Terminal
echo ======================================================================
if exist "C:\Users\musu9\AppData\Local\Programs\Python\Python312\python.exe" (
    "C:\Users\musu9\AppData\Local\Programs\Python\Python312\python.exe" HM_start.py %*
) else (
    py -3.12 HM_start.py %*
)
pause
