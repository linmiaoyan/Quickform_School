@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

set GITHUB_URL=https://github.com/quickform_school/quickform_school.git

echo ============================================
echo 服务器代码更新
echo ============================================
echo.

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

REM 检查是否在Git仓库中
if not exist ".git" (
    echo [提示] 当前目录不是Git仓库，正在初始化...
    git init
    git branch -M main
    echo [成功] Git仓库已初始化
    echo.
)

REM 检查远程仓库是否已配置
git remote | findstr /C:"origin" >nul 2>&1
if %errorlevel% equ 0 (
    echo [成功] 远程仓库已配置
    for /f "tokens=*" %%a in ('git remote get-url origin 2^>nul') do set CURRENT_URL=%%a
    echo 当前远程地址: !CURRENT_URL!
    echo.
    
    REM 检查URL是否正确
    if not "!CURRENT_URL!"=="!GITHUB_URL!" (
        echo [提示] 远程地址不匹配，正在更新...
        git remote set-url origin !GITHUB_URL!
        echo [成功] 远程地址已更新
        echo.
    )
    
    echo [步骤] 正在拉取最新代码...
    git pull origin main
    
    if %errorlevel% equ 0 (
        echo.
        echo [成功] 代码更新成功
    ) else (
        echo.
        echo [错误] 代码更新失败
        pause
        exit /b 1
    )
) else (
    echo [提示] 远程仓库未配置，正在配置...
    git remote add origin !GITHUB_URL!
    echo [成功] 远程仓库已配置
    echo.
    
    echo [步骤] 正在拉取最新代码...
    git pull origin main
    
    if %errorlevel% equ 0 (
        echo.
        echo [成功] 代码更新成功
    ) else (
        echo.
        echo [错误] 代码更新失败
        echo.
        echo [提示] 如果这是首次配置，可能需要先推送代码到GitHub
        pause
        exit /b 1
    )
)

echo.
echo ============================================
pause


