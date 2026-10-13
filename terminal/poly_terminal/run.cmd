@echo off
REM ===========================================================================
REM  POLY // TERMINAL launcher  (ASCII only - do NOT put non-ASCII in this file:
REM  cmd.exe parses this before chcp 65001 takes effect and would break)
REM
REM  UTF-8 is mandatory: this box has chcp=936/GBK, without these you get
REM      UnicodeEncodeError: 'gbk' codec can't encode character '\xa2'   (the cent sign)
REM  Proxy is mandatory: the three polymarket domains only work via 127.0.0.1:7890
REM      (Clash / mihomo must be running). The CN market band works direct either way.
REM
REM  Usage:
REM      run.cmd --once            print one screen of plain text and exit
REM      run.cmd                   live mode, 1s repaint / 300s refetch
REM      run.cmd --once --color    same, with ANSI colour
REM ===========================================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set "PY=C:\Users\33096\.conda\envs\aiquant\python.exe"
if not exist "%PY%" (
  echo [POLY] python interpreter not found: %PY%
  exit /b 1
)

"%PY%" "%~dp0app.py" %*
