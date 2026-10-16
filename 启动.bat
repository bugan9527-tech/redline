@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先找无控制台窗口的 pythonw：这样后台跑起来不会有黑窗口一直挂着。
set "PYW="
for /f "delims=" %%I in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%I"
if not defined PYW if exist "D:\python\pythonw.exe" set "PYW=D:\python\pythonw.exe"

set "PY="
if not defined PYW (
  set "PY=py"
  %PY% --version >nul 2>nul || set "PY=python"
  %PY% --version >nul 2>nul || set "PY=D:\python\python.exe"
  %PY% --version >nul 2>nul || set "PY="
)

if not defined PYW if not defined PY (
  echo.
  echo 没找到 Python。请先装好 Python 3，再双击这个文件。
  echo.
  pause
  exit /b 1
)
if not exist "app\server.py" (
  echo.
  echo 找不到 app\server.py。请把这个文件和 app 文件夹放在同一层。
  echo.
  pause
  exit /b 1
)

if not defined PYW (
  rem 没有 pythonw 就退回前台跑：会有黑窗口，按 Ctrl+C 停。
  title 深浅 · 本地服务
  echo.
  echo 深浅 · 正在启动本地服务，浏览器会自动打开首页。
  echo 关掉这个黑窗口就是停止服务。
  echo.
  %PY% "app\server.py" --log "work\server.log"
  echo.
  echo 服务已停止。
  pause
  exit /b 0
)

rem 后台静默启动，浏览器会自己打开。
rem 如果已经在跑，server.py 探到之后只会把页面打开、不再起第二个，所以重复双击是安全的。
start "" "%PYW%" "app\server.py" --log "work\server.log"
exit /b 0
