@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

REM ============================================
REM QuickForm 一键更新 + 重启（不改系统代理）
REM - 仅本脚本中的 git 走 Watt Toolkit HTTP 代理
REM - 拉取代码后按端口结束旧进程，再启动 app_waitress.py
REM - 最后访问 /ping 做健康检查
REM ============================================

cd /d "%~dp0"

REM ---- 可配置项（默认匹配你当前环境） ----
set "GITHUB_URL=https://github.com/linmiaoyan/QuickForm.git"
set "BRANCH=main"

REM Watt Toolkit 代理（HTTP 代理，端口来自你截图：26561）
set "WT_PROXY_HOST=127.0.0.1"
set "WT_PROXY_PORT=26561"

REM Waitress/Flask 监听端口（默认 5000；如你改过 .env 的 FLASK_PORT，可同步改这里）
set "APP_PORT=5000"
set "APP_HOST=127.0.0.1"

REM Python 启动命令（若你的 python 不在 PATH，可改成绝对路径）
set "PYTHON=python"
set "APP_ENTRY=app_waitress.py"

REM ---- git 仅对本脚本启用代理（不写入全局配置）----
set "GIT_PROXY=http://%WT_PROXY_HOST%:%WT_PROXY_PORT%"
set "GIT_PROXY_OPTS=-c http.proxy=%GIT_PROXY% -c https.proxy=%GIT_PROXY%"

echo ============================================
echo QuickForm 一键更新 + 重启
echo ============================================
echo.
echo [信息] GitHub: %GITHUB_URL%
echo [信息] 分支: %BRANCH%
echo [信息] git 临时代理: %GIT_PROXY%
echo [信息] 服务地址: http://%APP_HOST%:%APP_PORT%
echo.

REM ---- 预检查：Git 是否存在 ----
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Git，请先安装 Git for Windows
    pause
    exit /b 1
)

REM ---- 预检查：Python 是否存在 ----
%PYTHON% --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python（%PYTHON%）。请确认已安装并加入 PATH
    pause
    exit /b 1
)

REM ---- 预检查：通过代理访问 GitHub ----
echo [预检查] 正在通过代理检测 GitHub 连通性...
powershell -NoProfile -Command ^
  "try { $p='http://%WT_PROXY_HOST%:%WT_PROXY_PORT%'; $proxy=New-Object System.Net.WebProxy($p,$true); $wc=New-Object System.Net.WebClient; $wc.Proxy=$proxy; $wc.Headers.Add('User-Agent','QuickForm-Deploy'); $wc.DownloadString('https://github.com') | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 无法通过代理连接 GitHub，请检查：
    echo   1. Watt Toolkit 是否已开启加速（代理端口 %WT_PROXY_PORT% 可用）
    echo   2. 端口是否被占用或被防火墙拦截
    echo.
    pause
    exit /b 1
)
echo [成功] GitHub 连通性正常（走代理）
echo.

REM ---- 初始化/校验 Git 仓库 ----
if not exist ".git" (
    echo [提示] 当前目录不是 Git 仓库，正在初始化...
    git init
    git branch -M %BRANCH%
    echo [成功] Git 仓库已初始化
    echo.
)

git remote | findstr /C:"origin" >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 远程仓库未配置，正在配置...
    git remote add origin %GITHUB_URL%
    echo [成功] 远程仓库已配置
    echo.
) else (
    for /f "tokens=*" %%a in ('git remote get-url origin 2^>nul') do set "CURRENT_URL=%%a"
    if not "!CURRENT_URL!"=="%GITHUB_URL%" (
        echo [提示] 远程地址不匹配，正在更新...
        git remote set-url origin %GITHUB_URL%
        echo [成功] 远程地址已更新
        echo.
    )
)

REM ---- 拉取最新代码（仅 git 走代理）----
echo [步骤1] 拉取最新代码（git 走代理）...
git %GIT_PROXY_OPTS% pull origin %BRANCH%
if %errorlevel% neq 0 (
    echo [错误] 代码拉取失败
    pause
    exit /b 1
)
echo [成功] 代码已更新
echo.

REM ---- 重启：按端口找到并结束旧进程 ----
echo [步骤2] 结束占用端口 %APP_PORT% 的旧进程...
set "PIDS="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":%APP_PORT% .*LISTENING"') do (
    if not "%%p"=="0" (
        set "PIDS=!PIDS! %%p"
    )
)

if "%PIDS%"=="" (
    echo [提示] 未发现端口 %APP_PORT% 的监听进程
) else (
    echo [信息] 将结束 PID:%PIDS%
    for %%p in (%PIDS%) do (
        taskkill /PID %%p /F >nul 2>&1
    )
    echo [成功] 已尝试结束旧进程
)
echo.

REM ---- 启动 waitress（后台启动，输出日志）----
if not exist "logs" (
    mkdir logs >nul 2>&1
)

echo [步骤3] 启动服务（后台）...
set "LOG_OUT=logs\\waitress-stdout.log"
set "LOG_ERR=logs\\waitress-stderr.log"

REM 用 cmd /c 做重定向，start /b 后台运行
start "" /b cmd /c "%PYTHON% %APP_ENTRY% 1>>\"%LOG_OUT%\" 2>>\"%LOG_ERR%\""

REM 给服务一点启动时间
timeout /t 2 /nobreak >nul

REM ---- 健康检查 /ping ----
echo [步骤4] 健康检查 /ping ...
powershell -NoProfile -Command ^
  "try { $r = Invoke-WebRequest -Uri 'http://%APP_HOST%:%APP_PORT%/ping' -UseBasicParsing -TimeoutSec 8; if ($r.Content -match 'pong') { exit 0 } else { exit 2 } } catch { exit 1 }" >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 健康检查失败。请查看日志：
    echo   - %LOG_OUT%
    echo   - %LOG_ERR%
    echo 并检查：
    echo   1. .env 是否包含 SECRET_KEY 且不是弱默认值
    echo   2. 端口 %APP_PORT% 是否被其他程序占用
    echo   3. Python 依赖是否已安装（requirements.txt）
    echo.
    pause
    exit /b 1
)

echo ============================================
echo [完成] 更新并重启成功（pong）
echo ============================================
echo.
echo 当前提交:
git log -1 --oneline
echo.
pause

