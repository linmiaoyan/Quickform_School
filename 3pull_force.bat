@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

set GITHUB_URL=https://github.com/quickform_school/quickform_school.git

echo ============================================
echo 强制完整拉取 - 恢复所有文件
echo ============================================
echo.
echo [警告] 此操作将：
echo   1. 丢弃所有本地未提交的修改
echo   2. 恢复所有被删除的文件
echo   3. 使本地仓库与远程完全一致
echo.
set /p confirm="确认继续？(Y/N): "
if /i not "!confirm!"=="Y" (
    echo 操作已取消
    pause
    exit /b 0
)
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
    echo [错误] 当前目录不是Git仓库
    pause
    exit /b 1
)

REM 检查远程仓库是否已配置
git remote | findstr /C:"origin" >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 远程仓库未配置，正在配置...
    git remote add origin !GITHUB_URL!
    echo [成功] 远程仓库已配置
    echo.
)

REM 获取最新的远程状态
echo [步骤1] 获取最新远程状态...
git fetch origin
if %errorlevel% neq 0 (
    echo [错误] 获取远程状态失败
    pause
    exit /b 1
)
echo [成功] 已获取最新远程状态
echo.

REM 强制重置到远程main分支（这会恢复所有文件）
echo [步骤2] 强制重置到远程main分支...
git reset --hard origin/main
if %errorlevel% neq 0 (
    echo [错误] 重置失败
    pause
    exit /b 1
)
echo [成功] 已重置到远程main分支
echo.

REM 清理未跟踪的文件（可选，如果需要完全同步）
echo [步骤3] 清理未跟踪的文件和目录...
git clean -fd
if %errorlevel% neq 0 (
    echo [警告] 清理过程可能有问题，但主要文件已恢复
)
echo [成功] 清理完成
echo.

echo ============================================
echo [完成] 所有文件已完整恢复！
echo ============================================
echo.
echo 提示：如果某些文件仍未恢复，可能是因为：
echo   1. 这些文件在.gitignore中（如*.db文件）
echo   2. 这些文件从未被提交到Git仓库
echo.
pause

