@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ==========================================================
rem 找 Python。三条规矩，都是为了让它在别的机器上不"静默失败"：
rem   1) 先认 py 启动器。从微软商店装的"假 python"不附带 py.exe，
rem      所以能通过 py 的就是真家伙，天然躲开商店别名。
rem   2) 兜底才用 where python，并且必须排掉 WindowsApps 里那个别名：
rem      运行它只会弹出应用商店，用户看到的是"双击了没反应"，
rem      连报错都没有，比报错还难查。
rem   3) pythonw 直接从 python.exe 同一个目录推出来，不再 where 一次，
rem      也就不会再撞上别名。
rem ==========================================================
set "PY="
set "TMPF=%TEMP%\_qs_py_path.txt"
py -3 -c "import sys;print(sys.executable)" > "%TMPF%" 2>nul
if exist "%TMPF%" set /p PY=<"%TMPF%"
del "%TMPF%" 2>nul

if not defined PY (
  for /f "delims=" %%I in ('where python 2^>nul') do (
    if not defined PY (
      set "CAND=%%I"
      set "ALIAS="
      if not "!CAND:\WindowsApps\=!"=="!CAND!" set "ALIAS=1"
      if not defined ALIAS set "PY=!CAND!"
    )
  )
)

rem 版本闸门：低于 3.10 一律当没装。
rem 这一行必须留在块外——条件表达式里的括号会破坏 if ( ... ) 的块结构。
if not defined PY goto :no_python
"%PY%" -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto :no_python

rem pythonw 从同一个目录推出来，优先无窗口后台跑
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW="

if not exist "app\server.py" goto :no_files
if not defined PYW goto :foreground

rem 后台静默启动，浏览器会自己打开。
rem 已经在跑就不会起第二个：server.py 探到之后只把页面打开，所以重复双击是安全的。
start "" "%PYW%" "app\server.py" --log "work\server.log"
exit /b 0

:foreground
rem 没有 pythonw 就退回前台跑：会有黑窗口，按 Ctrl+C 停。
title 深浅 · 本地服务
echo.
echo 深浅 · 正在启动本地服务，浏览器会自动打开首页。
echo 关掉这个黑窗口就是停止服务。
echo.
"%PY%" "app\server.py" --log "work\server.log"
echo.
echo 服务已停止。
pause
exit /b 0

:no_python
echo.
echo 没找到可用的 Python（深浅需要 3.10 或更高版本）。
echo 装好 Python 后，还要在项目目录执行一次依赖安装：
echo     python -m pip install -r requirements.txt
echo.
echo 这就帮你打开下载页。安装时请务必勾上 Add python.exe to PATH 这一项。
echo 装好之后关掉本窗口，重新双击 启动.bat 就行。如果还是不行，
echo 多半是装的时候漏了那一步，卸掉重装一次即可。
echo.
start "" "https://www.python.org/downloads/"
pause
exit /b 1

:no_files
echo.
echo 找不到 app\server.py。请把这个文件和 app 文件夹放在同一层。
echo.
pause
exit /b 1
