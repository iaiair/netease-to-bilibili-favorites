@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo  bili_sync.exe 重新打包脚本
echo ============================================
echo.

set "VENV=%~dp0..\..\work\build-env"

if not exist "%VENV%\Scripts\python.exe" (
    echo [1/3] 创建打包用的虚拟环境...
    python -m venv "%VENV%" || goto :fail
    "%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet || goto :fail
) else (
    echo [1/3] 复用已有虚拟环境
)

echo [2/3] 安装/检查 PyInstaller...
"%VENV%\Scripts\python.exe" -m pip install pyinstaller --quiet || goto :fail

echo [3/3] 开始打包...
"%VENV%\Scripts\python.exe" -m PyInstaller --onefile --console --name bili_sync ^
    --distpath "%~dp0" --workpath "%~dp0..\..\work\pyi-build" ^
    --specpath "%~dp0..\..\work\pyi-build" --noconfirm ^
    --exclude-module unittest --exclude-module pydoc --exclude-module xmlrpc ^
    "%~dp0bili_app.py" || goto :fail

echo.
echo 打包完成：%~dp0bili_sync.exe
pause
exit /b 0

:fail
echo.
echo 打包失败，请把上面的错误信息发给我。
pause
exit /b 1
