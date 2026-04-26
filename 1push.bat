@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ============================================
echo Git 提交并推送
echo ============================================
echo.

cd /d "%~dp0"

REM 检查Git是否安装
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Git，请先安装 Git for Windows
    pause
    exit /b 1
)

REM 预检查：检测与 GitHub 的连接
echo [预检查] 正在检测与 GitHub 的连接...
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'https://github.com' -UseBasicParsing -TimeoutSec 10; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 无法连接 GitHub，请检查：
    echo   1. 网络是否正常
    echo   2. 是否需要配置代理
    echo   3. 防火墙是否允许访问 github.com
    echo.
    pause
    exit /b 1
)
echo [成功] GitHub 连接正常
echo.

echo [步骤1] 添加所有文件
git add .
if %errorlevel% neq 0 (
    echo [错误] 添加文件失败
    pause
    exit /b 1
)
echo [成功] 文件已添加到暂存区
echo.

:echo [步骤2] 提交更改
echo.

REM 检查是否有需要提交的更改（包括已暂存的）
git diff --cached --quiet
if %errorlevel% equ 0 (
    echo [提示] 当前没有新的更改需要提交，将直接尝试推送已有提交...
    echo.
    goto PUSH_STEP
)

set /p commit_msg="请输入提交描述: "

if "!commit_msg!"=="" (
    echo [错误] 提交描述不能为空
    pause
    exit /b 1
)

git commit -m "!commit_msg!"
if %errorlevel% neq 0 (
    echo [错误] 提交失败
    pause
    exit /b 1
)
echo [成功] 代码已提交
echo.

:PUSH_STEP
echo [步骤3] 推送到GitHub
git push origin main
if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo [成功] 代码已推送到GitHub
    echo ============================================
    echo.
    echo 仓库地址：https://github.com/linmiaoyan/quickform_school
    echo.
) else (
    echo.
    echo [错误] 推送失败
    echo.
    echo 可能的原因：
    echo 1. 网络连接问题
    echo 2. 认证失败（需要Personal Access Token）
    echo 3. 权限不足
    echo.
)

pause
