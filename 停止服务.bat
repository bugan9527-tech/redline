@echo off
chcp 65001 >nul
title 深浅 · 停止服务

rem 按端口找进程，别按进程名杀（pythonw 可能还跑着别的程序）。
set "KILLED="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  taskkill /f /pid %%P >nul 2>nul && set "KILLED=1"
)

if defined KILLED (
  echo.
  echo 深浅已经停了。
  echo.
) else (
  echo.
  echo 8765 端口上没有在跑的服务，什么都没做。
  echo.
)
echo.
echo 按任意键关闭。
pause >nul
