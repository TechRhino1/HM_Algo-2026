@echo off
title HM Algo 2.0 Trading Terminal
cd /d "%~dp0"
set "PYTHONPATH=C:\Users\musu9\AppData\Roaming\Python\Python315\site-packages;%PYTHONPATH%"
echo ======================================================================
echo           Starting HM Algo 2.0 Live Trading Terminal
echo ======================================================================
python HM_start.py %*
pause
