@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ============================================
echo Git 本地回退（reset --hard）
echo ============================================
echo.
echo [说明] 将把当前分支强制指向你指定的提交，工作区与暂存区会一并回到该提交的状态。
echo        该提交之后的本地提交记录将不再存在于当前分支（文件内容会恢复为旧版）。
echo        本脚本不会执行 git push；若错误已推送到远程，需另行处理（如强推或 revert）。
echo.

cd /d "%~dp0"

git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Git，请先安装 Git for Windows
    pause
    exit /b 1
)

if not exist ".git" (
    echo [错误] 当前目录不是 Git 仓库
    pause
    exit /b 1
)

echo [最近提交] （供参考，可复制完整哈希）
echo --------------------------------------------
git log -12 --oneline --decorate
echo --------------------------------------------
echo.

set "ROLLBACK_REF="
set /p ROLLBACK_REF="请输入回退目标（留空则使用 HEAD~1，即回退 1 个提交）: "
if "!ROLLBACK_REF!"=="" set "ROLLBACK_REF=HEAD~1"

echo.
echo 即将执行: git reset --hard !ROLLBACK_REF!
echo.
set /p confirm="确认继续？(Y/N): "
if /i not "!confirm!"=="Y" (
    echo 操作已取消
    pause
    exit /b 0
)
echo.

git reset --hard !ROLLBACK_REF!
if %errorlevel% neq 0 (
    echo.
    echo [错误] 回退失败，请检查提交哈希或引用是否正确（例如是否已 fetch 远程分支）。
    pause
    exit /b 1
)

echo.
echo ============================================
echo [完成] 已回退到: !ROLLBACK_REF!
echo ============================================
echo.
echo 当前提交:
git log -1 --oneline
echo.
pause
