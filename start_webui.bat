@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo.
echo  超星学习控制台
echo  打开浏览器访问 http://127.0.0.1:8080
echo  保留本窗口，按 Ctrl+C 结束服务。
echo.
where py >nul 2>nul
if errorlevel 1 (
  python webui.py --open-browser
) else (
  py -3 webui.py --open-browser
)
if errorlevel 1 (
  echo.
  echo 启动失败。请确认已安装 Python 3.13+，并执行：
  echo python -m pip install -r requirements.txt
  pause
)
endlocal
