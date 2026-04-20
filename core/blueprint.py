"""
QuickForm Blueprint
将QuickForm改造为Blueprint，可以整合到主应用中
"""
import os
import json
import hashlib
import math
import random
import re
import secrets
import string
import threading
import html
import base64
import uuid
from urllib.parse import unquote_plus, quote as url_quote, urlsplit, urlunsplit, parse_qsl, urlencode
import zipfile
from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, make_response, send_file, send_from_directory, current_app, abort
from werkzeug.datastructures import FileStorage
from sqlalchemy import create_engine, or_, text, func
from sqlalchemy.exc import IntegrityError, DataError
from sqlalchemy.orm import sessionmaker, scoped_session, joinedload
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta
import pandas as pd
import io
import matplotlib
matplotlib.use('Agg')  # 使用非交互式后端
import matplotlib.pyplot as plt
from dotenv import load_dotenv
import logging
from functools import wraps
from collections import deque
from typing import Deque, Optional
import time
import unicodedata
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
import subprocess

# 导入分离的模块
from .models import (
    Base, User, Task, Submission, AIConfig, migrate_database, CertificationRequest, Post, PostReply,
    Organization, OrganizationMember, TaskShare, TaskLike, ApiAccessLog, TaskQuotaRequest,
    SiteQuotaDefault, OneclickPromptOption, DEFAULT_ONECLICK_PROMPT_OPTIONS, _generate_task_id,
)
from .secret_store import decrypt_ai_config_inplace, encrypt_ai_config_inplace
from .public_errors import MSG_GENERIC, MSG_SERVICE_BUSY, MSG_JSON_BODY, MSG_SAVE_FAILED, MSG_PAGE_LOAD, MSG_API_INTERNAL
from .i18n import translate
from .login_throttle import login_blocked, record_login_failure, clear_login_throttle
from .project_usage import get_top_projects, evaluate_project_alerts
from .client_ip import get_request_client_ip
from services.file_service import save_uploaded_file, read_file_content, ALLOWED_EXTENSIONS, allowed_file, CERTIFICATION_ALLOWED_EXTENSIONS
from services.ai_service import (
    call_ai_model,
    generate_analysis_prompt,
    analyze_html_file,
    generate_html_page_from_prompt,
    revise_html_with_ai,
    get_chat_server_model_light,
)
from services.report_service import (
    save_analysis_report, generate_report_image, build_report_html, perform_analysis_with_custom_prompt,
    analysis_progress, analysis_results, completed_reports, progress_lock, timeout, markdown_to_html,
    _to_user_friendly_ai_error,
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def _redirect_back(fallback_endpoint: str = 'quickform.community', **fallback_kwargs):
    """优先返回来源页，避免操作后回到第一页。"""
    try:
        ref = (request.referrer or '').strip()
        if ref:
            return redirect(ref)
    except Exception:
        pass
    return redirect(url_for(fallback_endpoint, **(fallback_kwargs or {})))

# ---------- 服务就绪标记（用于维护页兜底）----------
_QUICKFORM_READY = False

# ---------- 更新日志（git log）缓存 ----------
_CHANGELOG_CACHE = {
    'ts': 0.0,
    'items': [],  # [{hash, date, subject, commit_url}]
    'error': '',
}

# ---------- 实时在线（应用层“当前正在处理的网页请求”） ----------
_ONLINE_LOCK = threading.Lock()
_ONLINE_INFLIGHT_WEB = 0


def _is_web_page_request(req) -> bool:
    """是否计入“老师端网页在线”：排除 /api/*、静态资源等高频请求。"""
    try:
        p = (req.path or '')
        if p.startswith('/static/') or p.startswith('/favicon') or p.startswith('/uploads/'):
            return False
        if p.startswith('/api/') or p.startswith('/cli') or p.startswith('/mcp'):
            return False
        # 仅统计 QuickForm blueprint 的页面端 endpoint
        ep = (req.endpoint or '')
        if not ep.startswith('quickform.'):
            return False
        # 明确排除纯 JSON 状态轮询等
        if ep in ('quickform.report_status', 'quickform.dashboard_status'):
            return False
        return True
    except Exception:
        return False


def _read_git_changelog(limit: int = 120):
    """从 git log 读取更新日志（短哈希+日期+标题）。若运行环境无 .git 则返回空并带 error。"""
    now_ts = time.time()
    ttl = 120.0  # 2 min cache
    try:
        if _CHANGELOG_CACHE.get('items') and (now_ts - float(_CHANGELOG_CACHE.get('ts') or 0.0) < ttl):
            return _CHANGELOG_CACHE.get('items') or [], _CHANGELOG_CACHE.get('error') or ''
    except Exception:
        pass

    items = []
    err = ''
    try:
        repo_root = _QUICKFORM_APP_ROOT  # core/..，一般即仓库根
        cmd = ['git', 'log', f'-n{max(1, int(limit))}', '--date=short', '--pretty=format:%h\t%ad\t%s']
        p = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=2.5)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or 'git log failed').strip())
        out = (p.stdout or '').strip()
        if out:
            for line in out.splitlines():
                parts = line.split('\t', 2)
                if len(parts) < 3:
                    continue
                h, d, s = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if not h:
                    continue
                items.append({
                    'hash': h,
                    'date': d,
                    'subject': s,
                    'commit_url': f'https://github.com/wstlab/quickform/commit/{h}',
                })
    except Exception as ex:
        err = f'无法读取 git 更新日志：{str(ex)}'
        items = []

    try:
        _CHANGELOG_CACHE['ts'] = now_ts
        _CHANGELOG_CACHE['items'] = items
        _CHANGELOG_CACHE['error'] = err
    except Exception:
        pass
    return items, err

# 加载环境变量
load_dotenv()


def _is_placeholder_or_empty_email(email):
    """未填邮箱或占位邮箱（注册时未填则存 username@noreply.local）视为未绑定"""
    if not email or not (email or '').strip():
        return True
    return (email or '').strip().endswith('@noreply.local')


_USER_EMAIL_FORMAT_RE = re.compile(r'^[^@]+@[^@]+\.[^@]+$')


def _normalize_admin_set_email(email_raw):
    """管理员设置用户邮箱：格式校验。返回 (email|None, 错误说明|None)。"""
    email = (email_raw or '').strip()
    if not email:
        return None, '邮箱不能为空'
    if not _USER_EMAIL_FORMAT_RE.match(email):
        return None, '邮箱格式不正确'
    return email, None


def _admin_apply_user_email_change(db, target_user, acting_user, new_email_raw):
    """
    在已打开的 db session 中修改目标用户邮箱（不 commit）。
    返回 (False, err_msg) 失败；(True, 'unchanged') 未改；(True, None) 已修改待 commit。
    """
    if not target_user or not acting_user:
        return False, '用户不存在'
    if target_user.id == acting_user.id:
        return False, '不能修改自己的邮箱，请在个人资料中修改'
    new_email, fmt_err = _normalize_admin_set_email(new_email_raw)
    if fmt_err:
        return False, fmt_err
    old = (target_user.email or '').strip()
    if old == new_email:
        return True, 'unchanged'
    if db.query(User).filter(User.email == new_email, User.id != target_user.id).first():
        return False, '该邮箱已被其他账号使用'
    target_user.email = new_email
    target_user.email_verified = False
    return True, None


def _email_requirement_block_for_next_task(db, user, task_count):
    """非管理员在已有至少 1 个任务时再创建新项目，须绑定真实邮箱并完成验证（第二个任务起）。"""
    if user.is_admin() or task_count < 1:
        return None
    refreshed_user = db.get(User, user.id)
    if not refreshed_user:
        return 'bind_email'
    if _is_placeholder_or_empty_email(refreshed_user.email):
        return 'bind_email'
    if not getattr(refreshed_user, 'email_verified', True):
        return 'verify_email'
    return None


def _org_members_can_edit_tasks(db, organization_id):
    """按组织ID读取成员是否可编辑任务，避免关系懒加载导致 DetachedInstanceError。"""
    if not organization_id:
        return False
    return bool(
        db.query(Organization.members_can_edit_tasks)
        .filter(Organization.id == organization_id)
        .scalar()
    )


def _user_can_edit_task_row(db, task, user_id):
    """与 edit_task 一致：所有者、管理员、组织内可编辑成员、被共享且带编辑权限。"""
    if not task or not user_id:
        return False
    actor = db.get(User, user_id)
    if actor and actor.is_admin():
        return True
    if task.user_id == user_id:
        return True
    share_record = db.query(TaskShare).filter_by(task_id=task.id, user_id=user_id).first()
    if share_record and share_record.can_edit:
        return True
    if task.organization_id:
        org_mem = db.query(OrganizationMember).filter_by(
            organization_id=task.organization_id,
            user_id=user_id,
        ).first()
        if org_mem and _org_members_can_edit_tasks(db, task.organization_id):
            return True
    return False


def _org_creator_id(db, organization_id):
    """按组织ID读取创建者ID，避免 task.organization 懒加载。"""
    if not organization_id:
        return None
    return db.query(Organization.creator_id).filter(Organization.id == organization_id).scalar()


# 简单的邮箱验证码存储（开发环境用，生产建议换成 Redis）
EMAIL_CODE_STORE = {}

# /mcp/upload 防爆破（按 IP+用户名）
UPLOAD_AUTH_WINDOW_SECONDS = 10 * 60
UPLOAD_AUTH_MAX_FAILS = 8
UPLOAD_AUTH_BLOCK_SECONDS = 15 * 60
_upload_auth_failures = {}
_upload_auth_lock = threading.Lock()


def set_email_code(email: str, code: str, ttl_seconds: int = 600):
    """保存邮箱验证码"""
    EMAIL_CODE_STORE[email] = {
        'code': code,
        'expires_at': time.time() + ttl_seconds
    }


def verify_email_code(email: str, code: str) -> bool:
    """校验邮箱验证码"""
    data = EMAIL_CODE_STORE.get(email)
    if not data:
        return False
    if time.time() > data['expires_at']:
        EMAIL_CODE_STORE.pop(email, None)
        return False
    if data['code'] != code:
        return False
    # 一次性验证码，用完即删
    EMAIL_CODE_STORE.pop(email, None)
    return True


def _upload_auth_client_key(username: str) -> str:
    ip = get_request_client_ip(request)
    return f"{ip}|{(username or '').strip().lower()}"


def _upload_auth_is_blocked(client_key: str) -> bool:
    now = time.time()
    with _upload_auth_lock:
        info = _upload_auth_failures.get(client_key)
        if not info:
            return False
        blocked_until = info.get('blocked_until', 0)
        if blocked_until and blocked_until > now:
            return True
        if blocked_until and blocked_until <= now:
            _upload_auth_failures.pop(client_key, None)
    return False


def _upload_auth_record_failure(client_key: str):
    now = time.time()
    with _upload_auth_lock:
        info = _upload_auth_failures.get(client_key)
        if not info or (now - info.get('window_start', now) > UPLOAD_AUTH_WINDOW_SECONDS):
            info = {'count': 0, 'window_start': now, 'blocked_until': 0}
        info['count'] = int(info.get('count', 0)) + 1
        if info['count'] >= UPLOAD_AUTH_MAX_FAILS:
            info['blocked_until'] = now + UPLOAD_AUTH_BLOCK_SECONDS
            logger.warning("上传认证触发限流：%s，失败次数=%s", client_key, info['count'])
        _upload_auth_failures[client_key] = info


def _upload_auth_clear_failures(client_key: str):
    with _upload_auth_lock:
        _upload_auth_failures.pop(client_key, None)


def _smtp_mail_envelope_address(raw: str) -> str:
    """
    将邮箱规范为 SMTP 会话命令（MAIL FROM / RCPT TO）可用的 ASCII 地址。
    smtplib 默认用 ascii 编码命令行；中文等国际域名需转为 Punycode，全角 @ 等需归一化。
    """
    if raw is None:
        return ''
    s = unicodedata.normalize('NFKC', (raw or '').strip())
    s = s.replace('\uFF20', '@')
    _, addr = parseaddr(s)
    addr = (addr or '').strip() or s
    addr = addr.strip()
    if '@' not in addr:
        raise ValueError('邮箱地址缺少 @')
    local, domain = addr.rsplit('@', 1)
    local, domain = local.strip(), domain.strip()
    if not local or not domain:
        raise ValueError('邮箱地址不完整')
    try:
        domain_ascii = domain.encode('idna').decode('ascii')
    except UnicodeError as e:
        raise ValueError('邮箱域名无法转为 ASCII（IDNA）') from e
    try:
        local.encode('ascii')
    except UnicodeError as e:
        raise ValueError('邮箱 @ 前本地部分含非 ASCII，当前 SMTP 通道不支持') from e
    return f'{local}@{domain_ascii}'


def send_email_code(to_email: str, code: str):
    """发送邮箱验证码"""
    try:
        conf = current_app.config
        sender = conf.get('MAIL_USERNAME')
        if not sender or not conf.get('MAIL_PASSWORD'):
            logger.error("邮件配置不完整，无法发送验证码")
            raise RuntimeError("邮件配置不完整")

        try:
            envelope_sender = _smtp_mail_envelope_address(sender)
            envelope_to = _smtp_mail_envelope_address(to_email)
        except ValueError as ve:
            logger.error(
                "邮箱地址无法用于 SMTP 信封: %s | MAIL_USERNAME=%r to=%r",
                ve,
                sender,
                to_email,
            )
            raise RuntimeError(
                "发信或收件邮箱格式异常（例如含中文域名且未正确编码），无法发送验证码，请检查系统发信邮箱与用户绑定邮箱。"
            ) from ve

        sender_name = "QuickForm 验证码"
        # 统一使用中性的标题，适用于注册、重置密码等场景
        subject = "QuickForm 验证码"
        body = (
            f"您的验证码是：{code}，有效期 10 分钟。\n\n"
            f"如果不是您本人在 QuickForm 中发起的操作，请忽略此邮件。"
        )

        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = formataddr((sender_name, envelope_sender))
        msg['To'] = envelope_to
        msg['Subject'] = subject

        server = None
        try:
            host = conf.get('MAIL_SERVER')
            port = conf.get('MAIL_PORT')

            # 统一策略：
            # - 465 端口：SMTP_SSL 直连（QQ/163 常用）
            # - 587/25 等端口：先 EHLO，再 STARTTLS
            if int(port) == 465:
                server = smtplib.SMTP_SSL(host, port)
            else:
                server = smtplib.SMTP(conf['MAIL_SERVER'], conf['MAIL_PORT'])
                server.ehlo()
                if conf.get('MAIL_USE_TLS', True):
                    server.starttls()
                    server.ehlo()

            try:
                server.login(conf['MAIL_USERNAME'], conf['MAIL_PASSWORD'])
            except smtplib.SMTPAuthenticationError as auth_err:
                # 针对 535 等认证错误给出更友好的提示，避免将底层错误直接暴露给前端
                logger.error(f"邮箱服务器认证失败，请检查 MAIL_USERNAME / MAIL_PASSWORD 是否为正确的邮箱账号及 SMTP 授权码: {auth_err}")
                raise RuntimeError("邮箱服务器认证失败，请联系管理员检查邮箱账号或授权码配置。")
            server.sendmail(envelope_sender, [envelope_to], msg.as_string())
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

        logger.info(f"验证码邮件已发送至: {to_email}")
    except RuntimeError:
        # 已由上方显式抛出，供接口层原样返回给用户
        raise
    except UnicodeEncodeError as e:
        # SMTP 命令行仅 ASCII：收件/发件地址含非 ASCII 且未规范化时会出现（多见于特殊域名或异常字符）
        logger.exception("发送邮箱验证码失败（SMTP 地址编码）: %s", e)
        raise RuntimeError(
            "验证码暂时无法发送：您绑定的邮箱格式与当前发信系统不兼容。请到个人资料中改为常用邮箱（如 QQ、163、Gmail），或联系管理员。"
        ) from e
    except Exception as e:
        logger.exception("发送邮箱验证码失败: %s", e)
        raise RuntimeError("验证码发送失败，请稍后再试。若多次失败请联系管理员。") from e


def send_username_reminder_email(to_email: str, usernames: str):
    """发送用户名提醒邮件（用于找回用户名；页面不直接回显以防枚举）"""
    try:
        conf = current_app.config
        sender = conf.get('MAIL_USERNAME')
        if not sender or not conf.get('MAIL_PASSWORD'):
            logger.error("邮件配置不完整，无法发送用户名提醒")
            raise RuntimeError("邮件配置不完整")

        try:
            envelope_sender = _smtp_mail_envelope_address(sender)
            envelope_to = _smtp_mail_envelope_address(to_email)
        except ValueError as ve:
            logger.error(
                "邮箱地址无法用于 SMTP 信封: %s | MAIL_USERNAME=%r to=%r",
                ve,
                sender,
                to_email,
            )
            raise RuntimeError(
                "发信或收件邮箱格式异常（例如含中文域名且未正确编码），无法发送邮件，请检查系统发信邮箱与用户绑定邮箱。"
            ) from ve

        sender_name = "QuickForm 账号助手"
        subject = "QuickForm 用户名提醒"
        body = (
            "您正在找回 QuickForm 用户名。\n\n"
            f"对应的用户名：{usernames}\n\n"
            "如果不是您本人发起的操作，请忽略此邮件。"
        )

        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = formataddr((sender_name, envelope_sender))
        msg['To'] = envelope_to
        msg['Subject'] = subject

        server = None
        try:
            host = conf.get('MAIL_SERVER')
            port = conf.get('MAIL_PORT')
            if int(port) == 465:
                server = smtplib.SMTP_SSL(host, port)
            else:
                server = smtplib.SMTP(conf['MAIL_SERVER'], conf['MAIL_PORT'])
                server.ehlo()
                if conf.get('MAIL_USE_TLS', True):
                    server.starttls()
                    server.ehlo()

            try:
                server.login(conf['MAIL_USERNAME'], conf['MAIL_PASSWORD'])
            except smtplib.SMTPAuthenticationError as auth_err:
                logger.error("邮箱服务器认证失败（用户名提醒）: %s", auth_err)
                raise RuntimeError("邮箱服务器认证失败，请联系管理员检查邮箱账号或授权码配置。")
            server.sendmail(envelope_sender, [envelope_to], msg.as_string())
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
        logger.info("用户名提醒邮件已发送至: %s", to_email)
    except RuntimeError:
        raise
    except Exception as e:
        logger.exception("发送用户名提醒邮件失败: %s", e)
        raise RuntimeError("邮件发送失败，请稍后再试。若多次失败请联系管理员。") from e


# 获取QuickForm目录路径
QUICKFORM_DIR = os.path.dirname(os.path.abspath(__file__))
# 独立应用根目录（与 app.py 所在目录一致）：docs/会议通知.pdf 等静态资料
_QUICKFORM_APP_ROOT = os.path.abspath(os.path.join(QUICKFORM_DIR, '..'))
MEETING_NOTICE_PDF_PATH = os.path.join(_QUICKFORM_APP_ROOT, 'docs', '会议通知.pdf')

# 创建上传文件目录（相对于QuickForm目录）- 保留原有目录，已上传文件继续由此路由提供
UPLOAD_FOLDER = os.path.join(QUICKFORM_DIR, 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# 新上传的任务 HTML 存到 static/uploads，由 Flask 静态服务直接访问，不经路由、不受限流
STATIC_UPLOADS = os.path.abspath(os.path.join(QUICKFORM_DIR, '..', 'static', 'uploads'))
if not os.path.exists(STATIC_UPLOADS):
    os.makedirs(STATIC_UPLOADS)


def _static_uploads_dir():
    """与 Flask 提供静态文件的目录一致，用于保存与判断是否走静态 URL"""
    try:
        if current_app.static_folder:
            d = os.path.abspath(os.path.join(current_app.static_folder, 'uploads'))
            if not os.path.exists(d):
                os.makedirs(d)
            return d
    except RuntimeError:
        pass
    return STATIC_UPLOADS

CERTIFICATION_FOLDER = os.path.join(UPLOAD_FOLDER, 'certifications')
MAX_HTML_FILE_SIZE = 4 * 1024 * 1024  # 任务内单个 HTML 文件最大 4MB
if not os.path.exists(CERTIFICATION_FOLDER):
    os.makedirs(CERTIFICATION_FOLDER)

# 允许的文件扩展名（仅HTML格式）
ALLOWED_EXTENSIONS = {'html', 'htm'}

def parse_urlencoded(raw_data):
    """手动解析URL编码的表单数据，避免Flask自动解析导致的问题"""
    result = {}
    if not raw_data:
        return result
    
    try:
        # 将bytes转为字符串
        if isinstance(raw_data, bytes):
            data_str = raw_data.decode('utf-8', errors='ignore')
        else:
            data_str = raw_data
        
        # 按&分割字段
        for pair in data_str.split('&'):
            if '=' in pair:
                key, value = pair.split('=', 1)
                key = unquote_plus(key)
                value = unquote_plus(value)
                result[key] = value
    except Exception as e:
        logger.error(f"解析URL编码数据失败: {str(e)}")
    
    return result

# 数据库配置（相对于QuickForm目录）
# 默认从环境变量读取，但可以通过init_quickform的参数强制指定
_database_type = None  # 将在init_quickform中设置

def _init_database(database_type=None):
    """初始化数据库连接"""
    global DATABASE_URL, engine, SessionLocal
    
    # 如果指定了数据库类型，使用指定的类型
    if database_type:
        if database_type.lower() == 'mysql':
            # 强制使用MySQL
            MYSQL_HOST = os.getenv('MYSQL_HOST', '')
            MYSQL_PORT = os.getenv('MYSQL_PORT', '3306')
            MYSQL_USER = os.getenv('MYSQL_USER', '')
            MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
            MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'quickform')
            
            if MYSQL_HOST and MYSQL_USER and MYSQL_PASSWORD:
                DATABASE_URL = f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4'
                logger.info(f"强制使用MySQL数据库: {MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}")
            else:
                logger.error("指定使用MySQL但环境变量未配置，回退到SQLite")
                DATABASE_URL = f'sqlite:///{os.path.join(QUICKFORM_DIR, "quickform.db")}'
                logger.info("使用SQLite数据库（MySQL配置缺失）")
        else:
            # 强制使用SQLite
            DATABASE_URL = f'sqlite:///{os.path.join(QUICKFORM_DIR, "quickform.db")}'
            logger.info("强制使用SQLite数据库")
    else:
        # 自动选择：优先使用环境变量中的MySQL配置，如果没有则使用SQLite
        MYSQL_HOST = os.getenv('MYSQL_HOST', '')
        MYSQL_PORT = os.getenv('MYSQL_PORT', '3306')
        MYSQL_USER = os.getenv('MYSQL_USER', '')
        MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
        MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'quickform')
        
        if MYSQL_HOST and MYSQL_USER and MYSQL_PASSWORD:
            # 使用MySQL
            DATABASE_URL = f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4'
            logger.info(f"使用MySQL数据库: {MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}")
        else:
            # 使用SQLite（向后兼容）
            DATABASE_URL = f'sqlite:///{os.path.join(QUICKFORM_DIR, "quickform.db")}'
            logger.info("使用SQLite数据库（向后兼容模式）")
    
    # 初始化SQLAlchemy引擎前，先清理旧的线程本地会话，避免重建连接后遗留会话占用连接
    global SessionLocal
    try:
        if SessionLocal and hasattr(SessionLocal, 'remove'):
            SessionLocal.remove()
    except Exception:
        pass

    # 初始化SQLAlchemy引擎
    mysql_connection_failed = False
    if DATABASE_URL.startswith('mysql'):
        # MySQL连接配置
        try:
            # 连接池参数（可通过环境变量覆盖）
            mysql_pool_size = int(os.getenv('MYSQL_POOL_SIZE', '20'))
            mysql_max_overflow = int(os.getenv('MYSQL_MAX_OVERFLOW', '40'))
            mysql_pool_timeout = int(os.getenv('MYSQL_POOL_TIMEOUT', '60'))
            mysql_pool_recycle = int(os.getenv('MYSQL_POOL_RECYCLE', '3600'))
            engine = create_engine(
                DATABASE_URL,
                pool_pre_ping=True,  # 自动重连
                pool_recycle=mysql_pool_recycle,   # 连接回收时间
                pool_size=mysql_pool_size,         # 常驻连接数
                max_overflow=mysql_max_overflow,   # 高峰临时连接数
                pool_timeout=mysql_pool_timeout,   # 获取连接超时时间（秒）
                pool_use_lifo=True,                # 优先复用最近释放连接，降低空闲断连概率
                echo=False
            )
            # 测试连接是否可用
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("MySQL连接测试成功")
        except Exception as e:
            logger.error(f"MySQL连接失败: {str(e)}，自动回退到SQLite")
            mysql_connection_failed = True
            # 回退到SQLite
            DATABASE_URL = f'sqlite:///{os.path.join(QUICKFORM_DIR, "quickform.db")}'
    
    # 如果MySQL连接失败或使用SQLite，初始化SQLite引擎
    if mysql_connection_failed or DATABASE_URL.startswith('sqlite'):
        # SQLite连接配置，启用外键约束
        def _fk_pragma_on_connect(dbapi_con, connection_record):
            """在SQLite连接时启用外键约束"""
            dbapi_con.execute('PRAGMA foreign_keys=ON')
        
        engine = create_engine(
            DATABASE_URL, 
            connect_args={'check_same_thread': False},
            poolclass=None  # SQLite不需要连接池
        )
        # 注册事件监听器，确保每次连接都启用外键约束
        from sqlalchemy import event
        event.listen(engine, 'connect', _fk_pragma_on_connect)
        if mysql_connection_failed:
            logger.info("已回退到SQLite数据库")
    
    SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

# 初始化数据库（默认行为，向后兼容）
_init_database()

MODEL_LABELS = {
    'chat_server': '硅基流动',
    'deepseek': 'DeepSeek',
    'doubao': '豆包',
    'qwen': '阿里云百炼',
    'moonshot': '月之暗面',
    'glm': '智谱清言',
    'ernie': '文心一言',
    'openrouter': 'OpenRouter'
}

# 注意：engine和SessionLocal现在在_init_database()函数中初始化

# 全局变量（将在init函数中设置）
bcrypt = None
login_manager = None

# 创建Blueprint
quickform_bp = Blueprint(
    'quickform',
    __name__,
    template_folder='templates',
    static_folder='../static'  # 指向主应用的static目录
)


@quickform_bp.teardown_request
def _cleanup_scoped_session(_exception=None):
    """请求结束后强制回收线程本地会话，避免连接泄漏导致连接池耗尽。"""
    try:
        if SessionLocal and hasattr(SessionLocal, 'remove'):
            SessionLocal.remove()
    except Exception:
        # 清理过程不影响主流程
        pass

    # 在线计数：请求结束时递减（teardown 一定会执行）
    try:
        if getattr(request, '_qf_counted_web', False):
            with _ONLINE_LOCK:
                global _ONLINE_INFLIGHT_WEB
                if _ONLINE_INFLIGHT_WEB > 0:
                    _ONLINE_INFLIGHT_WEB -= 1
    except Exception:
        pass

# 避免同一任务被并发触发“清空提交数据”导致长时间锁等待
_submission_clear_locks = {}
_submission_clear_locks_guard = threading.Lock()

# 读接口短缓存：降低高频轮询对数据库的压力
API_READ_CACHE_TTL = max(1, int(os.getenv('API_READ_CACHE_TTL', '10')))
API_READ_CACHE_MAX_KEYS = max(100, int(os.getenv('API_READ_CACHE_MAX_KEYS', '1000')))
_api_read_cache = {}
_api_read_cache_lock = threading.Lock()
COMMUNITY_RANDOM_CACHE_TTL = max(10, int(os.getenv('COMMUNITY_RANDOM_CACHE_TTL', '600')))
_community_random_cache = {'items': None, 'expire_at': 0.0}
_community_random_cache_lock = threading.Lock()
TASK_DATA_CACHE_TTL_MIN = max(1, int(os.getenv('TASK_DATA_CACHE_TTL_MIN', '1')))
TASK_DATA_CACHE_TTL_MAX = max(TASK_DATA_CACHE_TTL_MIN, int(os.getenv('TASK_DATA_CACHE_TTL_MAX', '10')))
TASK_DATA_CACHE_HOT_WINDOW = max(5, int(os.getenv('TASK_DATA_CACHE_HOT_WINDOW', '15')))
TASK_DATA_CACHE_HOT_THRESHOLD = max(1, int(os.getenv('TASK_DATA_CACHE_HOT_THRESHOLD', '12')))
_task_data_count_cache = {}
_task_data_page_ids_cache = {}
_task_data_access_stats = {}
_task_data_cache_lock = threading.Lock()
ADMIN_DATA_STATS_CACHE_TTL = max(5, int(os.getenv('ADMIN_DATA_STATS_CACHE_TTL', '30')))
_admin_data_stats_cache = {'value': None, 'expire_at': 0.0}
_admin_data_stats_cache_lock = threading.Lock()


def _build_read_cache_key(scope, task_id):
    raw_qs = request.query_string.decode('utf-8', errors='ignore') if request.query_string else ''
    digest = hashlib.sha1(raw_qs.encode('utf-8')).hexdigest() if raw_qs else 'noqs'
    return f"{scope}:{task_id}:{digest}"


def _cache_read_get(cache_key):
    now_ts = time.time()
    with _api_read_cache_lock:
        rec = _api_read_cache.get(cache_key)
        if not rec:
            return None
        if rec['expire_at'] <= now_ts:
            _api_read_cache.pop(cache_key, None)
            return None
        return rec['payload']


def _cache_read_set(cache_key, payload, ttl=None):
    ttl = ttl or API_READ_CACHE_TTL
    now_ts = time.time()
    with _api_read_cache_lock:
        if len(_api_read_cache) >= API_READ_CACHE_MAX_KEYS:
            # 简单淘汰：先清掉过期项，再按最早过期时间淘汰一部分
            expired = [k for k, v in _api_read_cache.items() if v['expire_at'] <= now_ts]
            for k in expired:
                _api_read_cache.pop(k, None)
            if len(_api_read_cache) >= API_READ_CACHE_MAX_KEYS:
                oldest = sorted(_api_read_cache.items(), key=lambda item: item[1]['expire_at'])[:50]
                for k, _ in oldest:
                    _api_read_cache.pop(k, None)
        _api_read_cache[cache_key] = {'payload': payload, 'expire_at': now_ts + ttl}


def _invalidate_task_read_cache(task_id):
    if not task_id:
        return
    id_text = str(task_id)
    with _api_read_cache_lock:
        to_delete = [k for k in _api_read_cache.keys() if f":{id_text}:" in k]
        for k in to_delete:
            _api_read_cache.pop(k, None)


def _invalidate_task_data_cache(task_db_id):
    if not task_db_id:
        return
    with _task_data_cache_lock:
        _task_data_count_cache.pop(int(task_db_id), None)
        prefix = f"{int(task_db_id)}:"
        to_delete = [k for k in _task_data_page_ids_cache.keys() if k.startswith(prefix)]
        for k in to_delete:
            _task_data_page_ids_cache.pop(k, None)


def _get_dynamic_task_data_ttl(task_db_id, now_ts=None):
    now_ts = now_ts or time.time()
    with _task_data_cache_lock:
        q = _task_data_access_stats.get(int(task_db_id))
        if q is None:
            q = deque()
            _task_data_access_stats[int(task_db_id)] = q
        q.append(now_ts)
        while q and now_ts - q[0] > TASK_DATA_CACHE_HOT_WINDOW:
            q.popleft()
        # 访问密度高时拉高缓存秒数，低频时保持 1 秒近实时
        return TASK_DATA_CACHE_TTL_MAX if len(q) >= TASK_DATA_CACHE_HOT_THRESHOLD else TASK_DATA_CACHE_TTL_MIN


def _get_cached_community_random(db, force_refresh=False):
    now_ts = time.time()
    if not force_refresh:
        with _community_random_cache_lock:
            if _community_random_cache['items'] is not None and _community_random_cache['expire_at'] > now_ts:
                return _community_random_cache['items']

    base_public = db.query(Task).filter(Task.sharing_type == "public", Task.public_approved == 1)
    public_ids = [row[0] for row in base_public.with_entities(Task.id).all()]
    items = []
    if public_ids:
        k = min(3, len(public_ids))
        pick_ids = random.sample(public_ids, k)
        by_id = {t.id: t for t in db.query(Task).filter(Task.id.in_(pick_ids)).all()}
        for tid in pick_ids:
            t = by_id.get(tid)
            if t:
                items.append(
                    {
                        "task_id": t.id,
                        "title": t.title,
                        "preview_url": _task_first_html_preview_url(t),
                    }
                )

    with _community_random_cache_lock:
        _community_random_cache['items'] = items
        _community_random_cache['expire_at'] = now_ts + COMMUNITY_RANDOM_CACHE_TTL
    return items


def _get_admin_data_stats_cached(db, today_start, total_users, admin_users, total_tasks, total_submissions):
    now_ts = time.time()
    with _admin_data_stats_cache_lock:
        rec = _admin_data_stats_cache
        if rec['value'] is not None and rec['expire_at'] > now_ts:
            return dict(rec['value'])

    normal_users = db.query(User).filter_by(role='user').count()
    new_users_today = db.query(User).filter(User.created_at >= today_start).count()
    new_tasks_today = db.query(Task).filter(Task.created_at >= today_start).count()
    avg_tasks_per_user = total_tasks / total_users if total_users > 0 else 0
    new_submissions_today = db.query(Submission).filter(Submission.submitted_at >= today_start).count()
    avg_submissions_per_task = total_submissions / total_tasks if total_tasks > 0 else 0
    tasks_with_reports = db.query(Task).filter(Task.analysis_report.isnot(None)).count()
    report_generation_rate = (tasks_with_reports / total_tasks * 100) if total_tasks > 0 else 0
    total_organizations = db.query(Organization).count()
    total_org_members = db.query(OrganizationMember).count()
    tasks_in_organizations = db.query(Task).filter(Task.organization_id.isnot(None)).count()
    certified_users = db.query(User).filter(User.is_certified == True).count()
    public_tasks = db.query(Task).filter(Task.sharing_type == 'public').count()
    public_approved_tasks = db.query(Task).filter(Task.sharing_type == 'public', Task.public_approved == 1).count()
    total_task_shares = db.query(TaskShare).count()
    total_task_likes = db.query(TaskLike).count()
    ai_generated_tasks = db.query(Task).filter(Task.ai_generated == True).count()
    cert_requests_pending = db.query(CertificationRequest).filter(CertificationRequest.status == 0).count()
    total_posts = db.query(Post).count()
    total_post_replies = db.query(PostReply).count()

    value = {
        'normal_users': normal_users,
        'new_users_today': new_users_today,
        'new_tasks_today': new_tasks_today,
        'avg_tasks_per_user': avg_tasks_per_user,
        'new_submissions_today': new_submissions_today,
        'avg_submissions_per_task': avg_submissions_per_task,
        'tasks_with_reports': tasks_with_reports,
        'report_generation_rate': report_generation_rate,
        'total_organizations': total_organizations,
        'total_org_members': total_org_members,
        'tasks_in_organizations': tasks_in_organizations,
        'certified_users': certified_users,
        'public_tasks': public_tasks,
        'public_approved_tasks': public_approved_tasks,
        'total_task_shares': total_task_shares,
        'total_task_likes': total_task_likes,
        'ai_generated_tasks': ai_generated_tasks,
        'cert_requests_pending': cert_requests_pending,
        'total_posts': total_posts,
        'total_post_replies': total_post_replies,
    }
    with _admin_data_stats_cache_lock:
        _admin_data_stats_cache['value'] = dict(value)
        _admin_data_stats_cache['expire_at'] = now_ts + ADMIN_DATA_STATS_CACHE_TTL
    return value


def _get_submission_clear_lock(task_id):
    with _submission_clear_locks_guard:
        if task_id not in _submission_clear_locks:
            _submission_clear_locks[task_id] = threading.Lock()
        return _submission_clear_locks[task_id]


def _file_in_static_uploads(saved_name):
    """判断文件是否在静态上传目录（static/uploads），用于生成静态 URL。"""
    if not saved_name:
        return False
    try:
        if os.path.exists(os.path.join(_static_uploads_dir(), saved_name)):
            return True
    except RuntimeError:
        pass
    if os.path.exists(os.path.join(STATIC_UPLOADS, saved_name)):
        return True
    return False


def _is_local_request_host(host_header: str) -> bool:
    """判断 Host 是否为本机开发常用名（此类场景不强行把 http 改成 https）。"""
    if not (host_header or '').strip():
        return True
    h = (host_header or '').split(',')[0].strip().split(':')[0].lower()
    return h in ('localhost', '127.0.0.1', '0.0.0.0', '[::1]')


def _public_site_base_url():
    """
    生成对外站点根 URL（含 scheme://host，无末尾斜杠），用于一键生成里嵌入的 API 根地址等。
    优先顺序：PUBLIC_BASE_URL（或 QUICKFORM_PUBLIC_BASE_URL）环境变量/应用配置
    → X-Forwarded-Proto / X-Forwarded-Host
    → 若配置了 PREFERRED_URL_SCHEME=https 且 Host 非本机，则把 http 升为 https（缓解 Nginx 未传 X-Forwarded-Proto 的情况）
    → request.host_url
    """
    cfg = (current_app.config.get('PUBLIC_BASE_URL') or '').strip().rstrip('/')
    if cfg:
        return cfg
    req = request
    xf_proto = (req.headers.get('X-Forwarded-Proto') or req.scheme or 'http').split(',')[0].strip().lower()
    xf_host = (req.headers.get('X-Forwarded-Host') or req.headers.get('Host') or '').split(',')[0].strip()
    host = xf_host or (getattr(req, 'host', None) or '')
    preferred = (current_app.config.get('PREFERRED_URL_SCHEME') or '').strip().lower()
    proto = xf_proto
    if preferred == 'https' and proto == 'http' and host and not _is_local_request_host(host):
        proto = 'https'
    if host:
        return f'{proto}://{host}'.rstrip('/')
    return (req.host_url or req.url_root or '').rstrip('/')


def _load_oneclick_prompt_tuples(db):
    """返回 [(opt_key, label, body), ...] 供一键生成与模板使用；表空时回退代码默认。"""
    try:
        rows = (
            db.query(OneclickPromptOption)
            .order_by(OneclickPromptOption.sort_order.asc(), OneclickPromptOption.id.asc())
            .all()
        )
    except Exception:
        rows = []
    if not rows:
        return list(DEFAULT_ONECLICK_PROMPT_OPTIONS)
    tuples = [(r.opt_key, r.label, (r.body or '').strip()) for r in rows]
    # 兜底：若数据库缺少新引入的默认选项（尚未迁移/未补齐），先在运行时补上
    existing_keys = {k for k, _l, _b in tuples}
    for k, l, b in DEFAULT_ONECLICK_PROMPT_OPTIONS:
        if k not in existing_keys:
            tuples.append((k, l, b))
    return tuples


def get_upload_file_url(saved_name, task_file_path=None):
    """返回上传文件的访问 URL：在 static/uploads 则用静态路径，否则走 /uploads/ 路由（保留原有文件）
    task_file_path: 可选，任务记录的 file_path；若该路径在 static 下则优先用静态 URL。
    """
    if not saved_name:
        return url_for('quickform.uploaded_file', filename='')
    try:
        # 1) 若调用方传入了 task.file_path 且当前文件即主文件，按路径是否在 static 下判断
        if task_file_path and saved_name == os.path.basename(task_file_path):
            norm = (task_file_path or '').replace('\\', '/')
            if 'static' in norm and 'uploads' in norm:
                return url_for('static', filename='uploads/' + saved_name)
        # 2) 文件实际存在于 static/uploads（避免 DB 路径与运行环境不一致时漏判）
        if _file_in_static_uploads(saved_name):
            return url_for('static', filename='uploads/' + saved_name)
    except RuntimeError:
        pass
    return url_for('quickform.uploaded_file', filename=saved_name)


def _append_query_param(url: str, key: str, value: str) -> str:
    """给 URL 安全追加 query 参数（保留原有 query/fragment；相同 key 则覆盖）。"""
    try:
        parts = urlsplit(url or '')
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        q[key] = value
        new_query = urlencode(q, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    except Exception:
        joiner = '&' if ('?' in (url or '')) else '?'
        return f"{url}{joiner}{key}={url_quote(value or '')}"


def _html_public_url_with_taskid(file_url: str, task_public_id: str, saved_name: str) -> str:
    """老师上传 HTML 的公网链接需要带 taskid（仅新生成链接；旧链接不追溯处理）。"""
    name = (saved_name or '').lower()
    if not (name.endswith('.html') or name.endswith('.htm')):
        return file_url
    if not task_public_id:
        return file_url
    return _append_query_param(file_url, 'taskid', task_public_id)


def _task_first_html_preview_url(task):
    """返回任务第一个可预览的 HTML 访问 URL（与任务详情页逻辑一致），供社区 iframe 等使用。"""
    if not task:
        return None
    saved_filename = None
    try:
        if task.file_path:
            saved_filename = os.path.basename(task.file_path)
    except Exception:
        saved_filename = None
    html_files = []
    if getattr(task, "html_files", None):
        try:
            html_files = json.loads(task.html_files)
        except Exception:
            html_files = []
    if not isinstance(html_files, list):
        html_files = []
    if task.file_name and saved_filename and not html_files:
        html_files = [{"saved_name": saved_filename, "original_name": task.file_name}]
    for f in html_files:
        if isinstance(f, dict) and f.get("saved_name"):
            return get_upload_file_url(f["saved_name"], task.file_path)
    return None


def _task_html_file_links(task):
    """任务下所有可访问的 HTML 附件（名称 + URL），与详情页多文件逻辑一致。"""
    if not task:
        return []
    saved_filename = None
    try:
        if task.file_path:
            saved_filename = os.path.basename(task.file_path)
    except Exception:
        saved_filename = None
    html_files = []
    if getattr(task, "html_files", None):
        try:
            html_files = json.loads(task.html_files)
        except Exception:
            html_files = []
    if not isinstance(html_files, list):
        html_files = []
    if task.file_name and saved_filename and not html_files:
        html_files = [{"saved_name": saved_filename, "original_name": task.file_name}]
    out = []
    seen = set()
    for f in html_files:
        if isinstance(f, dict) and f.get("saved_name"):
            sn = f["saved_name"]
            if sn in seen:
                continue
            seen.add(sn)
            out.append({
                "name": (f.get("original_name") or sn),
                "url": get_upload_file_url(sn, task.file_path),
            })
    return out


@quickform_bp.context_processor
def inject_upload_url():
    return dict(get_upload_file_url=get_upload_file_url)


@quickform_bp.before_app_request
def _maintenance_gate():
    """数据库迁移/初始化期间返回维护页，减少用户焦虑（仅 QuickForm 蓝图相关请求）。"""
    try:
        # 只在 QuickForm Blueprint 未就绪时生效
        if _QUICKFORM_READY:
            return None
        # 静态资源与健康检查不拦截
        p = (request.path or '')
        if p.startswith('/static/') or p.startswith('/favicon') or p.startswith('/healthz') or p.startswith('/cli'):
            return None
        # 只拦截本蓝图的 endpoint（避免影响主应用其它蓝图）
        ep = (request.endpoint or '')
        if not ep.startswith('quickform.'):
            return None
        return (
            render_template('maintenance.html'),
            503,
            {'Cache-Control': 'no-store'}
        )
    except Exception:
        return None


@quickform_bp.before_app_request
def _online_counter_before():
    """请求开始：统计当前正在处理的网页请求数（近似“实时在线”）。"""
    try:
        if not _is_web_page_request(request):
            return None
        setattr(request, '_qf_counted_web', True)
        with _ONLINE_LOCK:
            global _ONLINE_INFLIGHT_WEB
            _ONLINE_INFLIGHT_WEB += 1
    except Exception:
        pass
    return None

# 创建数据库表
Base.metadata.create_all(engine)

# 权限检查装饰器
def admin_required(f):
    """管理员权限检查装饰器"""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin():
            flash('您没有权限访问此页面', 'danger')
            return redirect(url_for('quickform.dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# 路由函数
@quickform_bp.route('/')
def index():
    """QuickForm首页（无论是否登录都展示统一首页）"""
    # 获取videos目录下的所有视频文件
    videos_dir = os.path.join(current_app.static_folder, 'videos')
    video_files = []
    if os.path.exists(videos_dir):
        for filename in os.listdir(videos_dir):
            if filename.lower().endswith(('.mp4', '.webm', '.ogg', '.mov')):
                video_files.append(filename)
        video_files.sort()  # 按文件名排序
    
    # 获取partners目录下的所有图片文件（支持PNG、JPG、JPEG格式）
    partners_dir = os.path.join(current_app.static_folder, 'partners')
    partner_logos = []
    if os.path.exists(partners_dir):
        for filename in os.listdir(partners_dir):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                partner_logos.append(filename)
        partner_logos.sort()  # 按文件名排序

    community_random = []
    db = SessionLocal()
    try:
        force_random_refresh = (request.args.get('refresh') or '').strip() == '1'
        community_random = _get_cached_community_random(db, force_refresh=force_random_refresh)
    finally:
        db.close()

    return render_template(
        'home.html',
        video_files=video_files,
        partner_logos=partner_logos,
        community_random=community_random,
    )


@quickform_bp.route('/changelog')
def changelog_page():
    """更新日志：不依赖部署环境 .git，使用写死的摘要（避免生产环境无 git 时空白/报错）。"""
    # 说明：此页内容由维护者定期根据 GitHub 提交整理后写入。
    groups = [
        {
            'date': '2026-04-16',
            'items': [
                {'subject': 'CLI/接口侧能力更新（api 邀请）。', 'hash': '1f80870', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/1f808700c65901c736f5188e8472248392729fe7'},
                {'subject': '接口相关调整（interface）。', 'hash': 'f082167', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/f082167a0accbd73250cd234a3173a12d555f490'},
                {'subject': '管理员面板与统计卡片相关更新（admin dashboard）。', 'hash': 'f5d806c', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/f5d806c8dc163d927391ec1b2b7455030881c641'},
                {'subject': '任务防护与批量删除相关更新（multi-del taskdefend）。', 'hash': 'fcf0437', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/fcf0437693f0984b77fa038fbc1cf971a2bf9921'},
            ],
        },
        {
            'date': '2026-04-15',
            'items': [
                {'subject': '社区与认证流程相关更新（qfcode community certification）。', 'hash': '2f3068a', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/2f3068aefdb51707b1b7f8893e3f24162f966276'},
            ],
        },
        {
            'date': '2026-04-12',
            'items': [
                {'subject': '首页/入口与展示细节调整（index）。', 'hash': '6ac361c', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/6ac361c54d069321aa0bd8fb96ff664d6049202a'},
                {'subject': '随机展示与阴影等 UI 细节优化（random shadow）。', 'hash': '5e65b05', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/5e65b054cb08af57b9e432b0b77a10fcc29d2464'},
            ],
        },
        {
            'date': '2026-04-10',
            'items': [
                {'subject': '数据详情页与展示修复（data detail update bug）。', 'hash': '87a9bff', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/87a9bffb1c4af5209ac48383a1d72a1ba955e904'},
                {'subject': '注册/登录相关测试与修复（sign up）。', 'hash': '47b7b3f', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/47b7b3f5c7cae8a3c4f335ccc29ce30f56323ca8'},
                {'subject': 'API Key 长度与配置兼容（api key longer）。', 'hash': 'f62147f', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/f62147fdd2515017fb8f9bf4fb61885cc237440a'},
                {'subject': '免责声明页面/文案更新（disclainer）。', 'hash': '81a608a', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/81a608afb29b8c4cbac3c830710a2ca3ca5517d3'},
            ],
        },
        {
            'date': '2026-04-09',
            'items': [
                {'subject': '全站 https 相关调整与修复。', 'hash': 'de1a52f', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/de1a52fad4af9712b45cf7473ef2400a84736f1c'},
                {'subject': '用户指纹/风控与异常禁止修复（user fingerprint）。', 'hash': 'fd87ffa', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/fd87ffadccdcc0f89faae13fda5591c3b4a6f8d1'},
                {'subject': '一键操作与错误修复（one click / error fix）。', 'hash': '71f922b', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/71f922b8bff7190e3a79d00c92be7e6056458999'},
                {'subject': 'waitress 与按钮灰度等细节修复。', 'hash': '1ac9681', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/1ac9681a7f39ab85dbcf7ebd8861341bc8f5be54'},
            ],
        },
        {
            'date': '2026-04-08',
            'items': [
                {'subject': '主色渐变与 UI 更新（UI gradient / UI update）。', 'hash': 'bb24de2', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/bb24de2632eb0e5288fc26b2d4a994270ff47e45'},
                {'subject': '服务端运行与 waitress 相关更新。', 'hash': '5750c89', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/5750c899d1eb697b1118d4c407f7b2a8ace6a642'},
            ],
        },
        {
            'date': '2026-04-07',
            'items': [
                {'subject': '页面布局与多处 bugfix；社区选择与懒加载修复。', 'hash': '7cb72d0', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/7cb72d06f6ddb05062af1902a7eba081beba32f2'},
                {'subject': '任务详情与加载优化（task detail / lazyload）。', 'hash': '3909a73', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/3909a73d9df64706e171c592bd97be9c01ead87e'},
                {'subject': '管理员 UI 与 SQL 缓存等更新。', 'hash': 'd8d20c8', 'commit_url': 'https://github.com/linmiaoyan/QuickForm/commit/d8d20c86e843186e84af847c7a5d4bd65c4cbea6'},
            ],
        },
    ]

    return render_template(
        'changelog.html',
        groups=groups,
        error='',
        limit=len(groups),
        now=datetime.now(),
    )

@quickform_bp.route('/switch_lang/<lang>')
def switch_lang(lang):
    """切换语言"""
    from core.i18n import set_locale
    if set_locale(lang):
        flash('语言已切换', 'success') if lang == 'zh-simple' else flash('Language switched', 'success')
    return redirect(request.referrer or url_for('quickform.index'))

@quickform_bp.route('/docs')
def docs():
    """官方文档 - 暂时留空"""
    return render_template('docs.html')

@quickform_bp.route('/tutorials')
def tutorials():
    """开源教程：读取 static/tutorials/tutorials.json 渲染列表，并保留 B 站视频区块"""
    tutorials_items = []
    try:
        tutorials_dir = os.path.join(current_app.static_folder, 'tutorials')
        json_path = os.path.join(tutorials_dir, 'tutorials.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                tutorials_items = data
            else:
                logger.warning('tutorials.json 根节点不是数组，已忽略')
    except Exception as e:
        logger.warning('读取 tutorials.json 失败: %s', e)
    for item in tutorials_items:
        if isinstance(item, dict):
            desc = (item.get('description') or '').strip()
            item['description_html'] = markdown_to_html(desc) if desc else ''
    return render_template('tutorials.html', tutorials_items=tutorials_items)

@quickform_bp.route('/cases')
def cases():
    """精品案例 - 暂时留空"""
    return render_template('cases.html')

@quickform_bp.route('/community')
def community():
    """项目交流：热榜需带 hot=1；留言板分页。随机公开项目预览在首页「随机发现」。"""
    db = SessionLocal()
    try:
        show_hot = (request.args.get("hot") or "").strip() == "1"
        per_project = max(1, min(20, request.args.get("per_project", 8, type=int)))
        per_post = max(1, min(50, request.args.get("per_post", 10, type=int)))
        page_latest = max(1, request.args.get("latest_page", 1, type=int))
        page_liked = max(1, request.args.get("liked_page", 1, type=int))
        page_posts = max(1, request.args.get("post_page", 1, type=int))

        base_public = db.query(Task).filter(Task.sharing_type == "public", Task.public_approved == 1)

        public_tasks_latest = []
        public_tasks_liked = []
        total_latest = 0
        total_liked = 0
        pages_latest = 1
        pages_liked = 1
        if show_hot:
            total_latest = base_public.count()
            total_liked = total_latest
            public_tasks_latest = (
                base_public.order_by(Task.created_at.desc())
                .offset((page_latest - 1) * per_project)
                .limit(per_project)
                .all()
            )
            public_tasks_liked = (
                db.query(Task)
                .filter(Task.sharing_type == "public", Task.public_approved == 1)
                .order_by(func.coalesce(Task.like_count, 0).desc(), Task.created_at.desc())
                .offset((page_liked - 1) * per_project)
                .limit(per_project)
                .all()
            )
            pages_latest = max(1, (total_latest + per_project - 1) // per_project) if total_latest else 1
            pages_liked = max(1, (total_liked + per_project - 1) // per_project) if total_liked else 1

        posts_query = (
            db.query(Post)
            .options(
                joinedload(Post.user),
                joinedload(Post.replies).joinedload(PostReply.user),
            )
            .order_by(
                func.coalesce(Post.is_pinned, 0).desc(),
                func.coalesce(Post.pinned_at, Post.created_at).desc(),
                Post.created_at.desc(),
            )
        )
        total_posts = posts_query.count()
        posts = posts_query.offset((page_posts - 1) * per_post).limit(per_post).all()
        pages_posts = max(1, (total_posts + per_post - 1) // per_post) if total_posts else 1

        pagination_latest = {"page": page_latest, "per_page": per_project, "pages": pages_latest, "total": total_latest}
        pagination_liked = {"page": page_liked, "per_page": per_project, "pages": pages_liked, "total": total_liked}
        pagination_posts = {"page": page_posts, "per_page": per_post, "pages": pages_posts, "total": total_posts}

        return render_template(
            "community.html",
            posts=posts,
            show_hot=show_hot,
            public_tasks_latest=public_tasks_latest,
            public_tasks_liked=public_tasks_liked,
            pagination_latest=pagination_latest,
            pagination_liked=pagination_liked,
            pagination_posts=pagination_posts,
        )
    finally:
        db.close()


@quickform_bp.route('/community/post/<int:post_id>/pin', methods=['POST'])
@login_required
def pin_post(post_id):
    """置顶/取消置顶留言（仅管理员）"""
    if not current_user.is_admin():
        flash('无权执行此操作', 'danger')
        return _redirect_back()
    db = SessionLocal()
    try:
        post = db.get(Post, post_id)
        if not post:
            flash('留言不存在', 'danger')
            return _redirect_back()
        want_pin = (request.form.get('pin') or '').strip()
        if want_pin == '1':
            post.is_pinned = True
            post.pinned_at = datetime.now()
            flash('已置顶该留言', 'success')
        elif want_pin == '0':
            post.is_pinned = False
            post.pinned_at = None
            flash('已取消置顶', 'success')
        else:
            # 未传 pin 参数则按当前状态切换
            if getattr(post, 'is_pinned', False):
                post.is_pinned = False
                post.pinned_at = None
                flash('已取消置顶', 'success')
            else:
                post.is_pinned = True
                post.pinned_at = datetime.now()
                flash('已置顶该留言', 'success')
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("置顶留言失败: %s", e)
        flash('操作失败', 'danger')
    finally:
        db.close()
    return _redirect_back()

@quickform_bp.route('/community/post', methods=['POST'])
@login_required
def create_post():
    """创建留言"""
    content = request.form.get('content', '').strip()
    if not content:
        flash('留言内容不能为空', 'danger')
        return _redirect_back()
    
    if len(content) > 2000:
        flash('留言内容过长，最多2000字符', 'danger')
        return _redirect_back()
    
    db = SessionLocal()
    try:
        post = Post(
            user_id=current_user.id,
            content=content,
            created_at=datetime.now()
        )
        db.add(post)
        db.commit()
        flash('留言发布成功', 'success')
    except Exception as e:
        db.rollback()
        logger.error(f"创建留言失败: {str(e)}")
        flash('留言发布失败', 'danger')
    finally:
        db.close()
    
    return _redirect_back()

@quickform_bp.route('/community/post/<int:post_id>/reply', methods=['POST'])
@login_required
def create_reply(post_id):
    """针对某条留言发表回复"""
    content = request.form.get('content', '').strip()
    if not content:
        flash('回复内容不能为空', 'danger')
        return _redirect_back()
    if len(content) > 2000:
        flash('回复内容过长，最多2000字符', 'danger')
        return _redirect_back()
    db = SessionLocal()
    try:
        post = db.get(Post, post_id)
        if not post:
            flash('该留言不存在', 'danger')
            return _redirect_back()
        reply = PostReply(
            post_id=post_id,
            user_id=current_user.id,
            content=content,
            created_at=datetime.now()
        )
        db.add(reply)
        db.commit()
        flash('回复发布成功', 'success')
    except Exception as e:
        db.rollback()
        logger.error(f"创建回复失败: {str(e)}")
        flash('回复发布失败', 'danger')
    finally:
        db.close()
    return _redirect_back()

@quickform_bp.route('/community/reply/<int:reply_id>/delete', methods=['POST'])
@login_required
def delete_reply(reply_id):
    """删除回复（仅管理员）"""
    if not current_user.is_admin():
        flash('无权执行此操作', 'danger')
        return _redirect_back()
    db = SessionLocal()
    try:
        reply = db.get(PostReply, reply_id)
        if reply:
            db.delete(reply)
            db.commit()
            flash('回复已删除', 'success')
        else:
            flash('回复不存在', 'danger')
    except Exception as e:
        db.rollback()
        logger.error(f"删除回复失败: {str(e)}")
        flash('删除失败', 'danger')
    finally:
        db.close()
    return _redirect_back()

@quickform_bp.route('/task/<int:task_id>/like', methods=['POST'])
@login_required
def task_like(task_id):
    """公开任务点赞/取消点赞（仅登录用户，仅对公开任务）"""
    db = SessionLocal()
    try:
        task = (
            db.query(Task)
            .options(
                joinedload(Task.author),
                joinedload(Task.organization),
            )
            .filter(Task.id == task_id)
            .first()
        )
        if not task or task.sharing_type != 'public':
            return jsonify({'success': False, 'message': '仅支持对公开项目点赞'}), 400
        existing = db.query(TaskLike).filter_by(task_id=task_id, user_id=current_user.id).first()
        if existing:
            db.delete(existing)
            task.like_count = max(0, (task.like_count or 0) - 1)
            liked = False
        else:
            db.add(TaskLike(task_id=task_id, user_id=current_user.id))
            task.like_count = (task.like_count or 0) + 1
            liked = True
        db.commit()
        return jsonify({'success': True, 'liked': liked, 'count': task.like_count or 0})
    except Exception as e:
        db.rollback()
        logger.exception("点赞操作失败: %s", e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()

@quickform_bp.route('/community/post/<int:post_id>/delete', methods=['POST'])
@login_required
def delete_post(post_id):
    """删除留言（仅管理员）"""
    if not current_user.is_admin():
        flash('无权执行此操作', 'danger')
        return _redirect_back()
    
    db = SessionLocal()
    try:
        post = db.get(Post, post_id)
        if post:
            db.delete(post)
            db.commit()
            flash('留言已删除', 'success')
        else:
            flash('留言不存在', 'danger')
    except Exception as e:
        db.rollback()
        logger.error(f"删除留言失败: {str(e)}")
        flash('删除失败', 'danger')
    finally:
        db.close()
    
    return _redirect_back()

@quickform_bp.route('/register', methods=['GET', 'POST'])
def register():
    """注册"""
    try:
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            email = (request.form.get('email') or '').strip()  # 注册可不填，空时用占位邮箱避免违反唯一约束
            password = request.form.get('password', '').strip()
            school = request.form.get('school', '').strip()
            phone = (request.form.get('phone') or '').strip() or None
            
            if not username or not password or not school:
                flash('请填写用户名、学校与密码', 'danger')
                return redirect(url_for('quickform.register'))

            if not request.form.get('agree_disclaimer'):
                flash('请先阅读并同意《免责声明》', 'danger')
                return redirect(url_for('quickform.register'))

            db = SessionLocal()
            try:
                from sqlalchemy import or_, func
                username_norm = username.lower()
                # 仅对非空手机号/邮箱做唯一性检查，避免 phone 为空时 OR 条件匹配大量用户导致误判或漏判
                # 用户名使用大小写无关查重，避免 A/a 被当成不同账号导致后续异常
                conditions = [func.lower(User.username) == username_norm]
                if phone:
                    conditions.append(User.phone == phone)
                if email:
                    conditions.append(User.email == email)
                existing_user = db.query(User).filter(or_(*conditions)).first()

                if existing_user:
                    if (existing_user.username or '').strip().lower() == username_norm:
                        flash('用户名已存在，请更换用户名或直接使用该账号登录', 'danger')
                    elif email and existing_user.email == email:
                        flash('邮箱已存在', 'danger')
                    else:
                        flash('手机号已被注册', 'danger')
                    return redirect(url_for('quickform.register'))

                hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
                # 邮箱未填时使用唯一占位符，避免多个空字符串违反 unique 约束
                email_value = email if email else f"{username}@noreply.local"
                if db.query(User).filter_by(email=email_value).first():
                    flash('该用户名对应的占位邮箱已被占用，请更换用户名', 'danger')
                    return redirect(url_for('quickform.register'))

                user = User(
                    username=username,
                    email=email_value,
                    password=hashed_password,
                    school=school,
                    phone=phone or '',
                )

                ai_config = AIConfig(user=user, selected_model='chat_server')

                db.add(user)
                try:
                    # 先 flush，提前触发唯一约束异常，避免前端误判“注册成功”
                    db.flush()
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    logger.warning("注册唯一约束冲突: username=%s email=%s", username, email_value)
                    # 冲突后再按字段精确提示，便于用户修正
                    try:
                        if db.query(User.id).filter(func.lower(User.username) == username_norm).first():
                            flash('用户名已存在，请更换用户名或直接使用该账号登录', 'danger')
                        elif email and db.query(User.id).filter(User.email == email).first():
                            flash('邮箱已存在', 'danger')
                        elif phone and db.query(User.id).filter(User.phone == phone).first():
                            flash('手机号已被注册', 'danger')
                        else:
                            flash('注册失败：用户名/邮箱/手机号存在冲突，请更换后重试', 'danger')
                    except Exception:
                        flash('用户名或邮箱已被占用，请更换后重试', 'danger')
                    return redirect(url_for('quickform.register'))

                flash('注册成功，请登录', 'success')
                return redirect(url_for('quickform.login'))
            finally:
                db.close()
        else:
            # GET请求，显示注册页面
            return render_template('register.html')
    except Exception as e:
        logger.exception("注册页面异常")
        flash(MSG_PAGE_LOAD, 'danger')
        try:
            return render_template('register.html')
        except Exception:
            return MSG_PAGE_LOAD, 500


@quickform_bp.route('/verify_email', methods=['GET', 'POST'])
@login_required
def verify_email():
    """创建第二个任务前验证邮箱：发送验证码到当前用户邮箱并校验"""
    next_url = request.args.get('next') or url_for('quickform.dashboard')
    db = SessionLocal()
    try:
        user = db.get(User, current_user.id)
        if not user or not user.email:
            flash('您的账号未绑定邮箱，请先在个人资料中填写邮箱后再验证。', 'danger')
            return redirect(url_for('quickform.profile'))
        if getattr(user, 'email_verified', False):
            return redirect(next_url)
        if request.method == 'POST':
            email_code = (request.form.get('email_code') or '').strip()
            if not email_code or not verify_email_code(user.email, email_code):
                flash('验证码错误或已过期，请重新获取', 'danger')
                return redirect(url_for('quickform.verify_email', next=next_url))
            user.email_verified = True
            db.commit()
            flash('邮箱验证成功，可以创建更多任务。', 'success')
            return redirect(next_url)
        return render_template('verify_email.html', next_url=next_url, email=user.email)
    finally:
        db.close()


@quickform_bp.route('/api/email/send_verify_code', methods=['POST'])
@login_required
def api_send_verify_code():
    """已登录用户：向当前用户绑定邮箱发送验证码（用于创建第二任务前的验证）"""
    try:
        db = SessionLocal()
        try:
            user = db.get(User, current_user.id)
            if not user or not user.email:
                return jsonify({'success': False, 'message': '您的账号未绑定邮箱'}), 400
            email = user.email
        finally:
            db.close()
        import random
        code = f"{random.randint(0, 999999):06d}"
        set_email_code(email, code, ttl_seconds=600)
        send_email_code(email, code)
        return jsonify({'success': True, 'message': '验证码已发送到您的邮箱，有效期10分钟'})
    except RuntimeError as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    except Exception as e:
        logger.exception("发送验证码异常: %s", e)
        return jsonify({'success': False, 'message': '验证码发送失败，请稍后再试。若持续失败请联系管理员。'}), 500


@quickform_bp.route('/login', methods=['GET', 'POST'])
def login():
    """登录"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = request.form.get('remember') == 'on'

        # 防撞库 / 暴力破解：按 IP（及 IP+登录名）限流，与 Session 内计数互补
        blocked, retry_after = login_blocked(request, username)
        if blocked:
            minutes = max(1, (retry_after + 59) // 60)
            flash(
                f'登录尝试过于频繁，请约 {minutes} 分钟后再试；若忘记密码请使用「忘记密码」。',
                'danger',
            )
            return render_template('login.html')
        
        db = SessionLocal()
        try:
            # 尝试多种方式查找用户：用户名、邮箱、手机号、昵称
            user = db.query(User).filter(
                (User.username == username) | 
                (User.email == username) | 
                (User.phone == username)
            ).first()
            
            if user and bcrypt.check_password_hash(user.password, password):
                # 登录成功，清除失败次数与 IP 限流计数
                from flask import session
                session.pop('login_fail_count', None)
                clear_login_throttle(request, username)
                login_user(user, remember=remember)
                # 将会话设为持久，使 session cookie 在 PERMANENT_SESSION_LIFETIME 内有效，重启服务后仍保持登录
                session.permanent = True
                next_page = request.args.get('next')
                return redirect(next_page) if next_page else redirect(url_for('quickform.dashboard'))
            else:
                record_login_failure(request, username)
                # 登录失败，检查失败次数（浏览器会话内提示，与 IP 限流并存）
                from flask import session
                fail_count = session.get('login_fail_count', 0) + 1
                session['login_fail_count'] = fail_count
                
                # 如果连续失败3次或以上，给出提示
                if fail_count >= 3:
                    flash('账号密码无特殊限制，您的账号可能为手机号或邮箱或昵称或姓名', 'info')
                else:
                    flash('用户名或密码错误', 'danger')
        finally:
            db.close()
    
    return render_template('login.html')


def _forgot_password_render_verify():
    """找回密码第二步：根据会话中的用户 ID 渲染验证页；无会话则回第一步。"""
    from flask import session

    user_id = session.get('pw_reset_user_id')
    if not user_id:
        return render_template('forgot_password.html', mode='start', user=None)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            session.pop('pw_reset_user_id', None)
            session.pop('pw_reset_code_email', None)
            return render_template('forgot_password.html', mode='start', user=None)
        email_is_placeholder = _is_placeholder_or_empty_email(user.email)
        return render_template(
            'forgot_password.html',
            mode='verify',
            user=user,
            email_is_placeholder=email_is_placeholder,
        )
    finally:
        db.close()


@quickform_bp.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    """
    通过手机号确认身份，再向绑定邮箱发送验证码以重置密码。
    第一步：输入手机号，查找账户并展示用户名；
    第二步：发送验证码并重置密码（绑定真实邮箱的账户使用库中邮箱；占位邮箱须先填写新邮箱再收码）。
    """
    from flask import session

    if request.method == 'POST':
        # 区分步骤：如果有phone字段，说明是第一步；否则为第二步重置密码
        phone = (request.form.get('phone') or '').strip()
        if phone:
            # 第一步：根据手机号查找用户
            db = SessionLocal()
            try:
                users = db.query(User).filter(User.phone == phone).all()
                if not users:
                    flash('未找到使用该手机号注册的账户', 'warning')
                    return redirect(url_for('quickform.forgot_password'))

                if len(users) > 1:
                    # 同一手机号多个账户时提示所有用户名，避免误操作
                    usernames = ', '.join(u.username for u in users)
                    flash(f'该手机号对应多个用户名：{usernames}，请联系管理员协助重置密码。', 'warning')
                    return redirect(url_for('quickform.forgot_password'))

                user = users[0]
                # 在会话中记录当前正在进行密码重置的用户ID
                session['pw_reset_user_id'] = user.id
                session.pop('pw_reset_code_email', None)
                # 跳转到第二步页面，展示用户名并允许发送验证码与重置密码
                email_is_placeholder = _is_placeholder_or_empty_email(user.email)
                return render_template(
                    'forgot_password.html',
                    mode='verify',
                    user=user,
                    email_is_placeholder=email_is_placeholder,
                )
            finally:
                db.close()
        else:
            # 第二步：根据会话中的用户信息校验验证码并重置密码
            email_code = (request.form.get('email_code') or '').strip()
            new_password = (request.form.get('new_password') or '').strip()
            confirm_password = (request.form.get('confirm_password') or '').strip()

            if not email_code or not new_password or not confirm_password:
                flash('请填写所有必填项', 'danger')
                return _forgot_password_render_verify()

            if new_password != confirm_password:
                flash('两次输入的密码不一致', 'danger')
                return _forgot_password_render_verify()

            if len(new_password) < 6:
                flash('新密码长度至少为6个字符', 'danger')
                return _forgot_password_render_verify()

            user_id = session.get('pw_reset_user_id')
            if not user_id:
                flash('重置流程已过期，请重新验证手机号', 'warning')
                return redirect(url_for('quickform.forgot_password'))

            db = SessionLocal()
            try:
                user = db.query(User).filter(User.id == user_id).first()
                if not user or not user.email:
                    flash('账户信息异常，请联系管理员', 'danger')
                    return redirect(url_for('quickform.forgot_password'))

                # 占位邮箱：验证码发往 session 中记录的「待绑定邮箱」
                code_email = (session.get('pw_reset_code_email') or '').strip()
                if _is_placeholder_or_empty_email(user.email):
                    if not code_email:
                        flash('请先填写真实邮箱并点击「发送验证码」', 'danger')
                        return _forgot_password_render_verify()
                    if not _USER_EMAIL_FORMAT_RE.match(code_email):
                        flash('邮箱格式不正确', 'danger')
                        return _forgot_password_render_verify()
                    if not verify_email_code(code_email, email_code):
                        flash('邮箱验证码错误或已过期，请重新获取', 'danger')
                        return _forgot_password_render_verify()
                    if db.query(User).filter(User.email == code_email, User.id != user.id).first():
                        flash('该邮箱已被其他账号使用，请更换', 'danger')
                        return _forgot_password_render_verify()
                    user.email = code_email
                    user.email_verified = True
                else:
                    if code_email and code_email != user.email:
                        session.pop('pw_reset_code_email', None)
                    if not verify_email_code(user.email, email_code):
                        flash('邮箱验证码错误或已过期，请重新获取', 'danger')
                        return _forgot_password_render_verify()

                hashed = bcrypt.generate_password_hash(new_password).decode('utf-8')
                user.password = hashed
                db.commit()
                # 完成后清理会话标记
                session.pop('pw_reset_user_id', None)
                session.pop('pw_reset_code_email', None)
                flash('密码重置成功，请使用新密码登录', 'success')
                return redirect(url_for('quickform.login'))
            finally:
                db.close()

    # GET：若会话中仍有重置中的用户，直接回到第二步（避免误刷新退回第一步）
    if session.get('pw_reset_user_id'):
        return _forgot_password_render_verify()
    return render_template('forgot_password.html', mode='start', user=None)


@quickform_bp.route('/forgot_password/send_code', methods=['POST'])
def forgot_password_send_code():
    """
    第二步中点击“发送验证码”时调用。
    已绑定真实邮箱：验证码发往库中邮箱（前端不可伪造目标地址）。
    占位邮箱：须通过 JSON 提交 new_email，校验格式与唯一后发往该邮箱，并在 session 中记录待校验地址。
    """
    from flask import session

    user_id = session.get('pw_reset_user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '重置流程已过期，请重新验证手机号'}), 400

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.email:
            return jsonify({'success': False, 'message': '账户信息异常，请联系管理员'}), 400

        payload = request.get_json(silent=True) or {}
        new_email = (payload.get('new_email') or '').strip()

        if _is_placeholder_or_empty_email(user.email):
            if not new_email:
                return jsonify({'success': False, 'message': '请先填写您的真实邮箱'}), 400
            if not _USER_EMAIL_FORMAT_RE.match(new_email):
                return jsonify({'success': False, 'message': '邮箱格式不正确'}), 400
            if new_email.endswith('@noreply.local'):
                return jsonify({'success': False, 'message': '请填写可收信的真实邮箱，不能使用系统占位域名'}), 400
            if db.query(User).filter(User.email == new_email, User.id != user.id).first():
                return jsonify({'success': False, 'message': '该邮箱已被其他账号使用，请更换'}), 400
            target = new_email
            session['pw_reset_code_email'] = new_email
        else:
            session.pop('pw_reset_code_email', None)
            target = (user.email or '').strip()
            if not target:
                return jsonify({'success': False, 'message': '账户信息异常，请联系管理员'}), 400

        code = f"{random.randint(0, 999999):06d}"
        set_email_code(target, code, ttl_seconds=600)
        send_email_code(target, code)
        return jsonify({'success': True, 'message': '验证码已发送'})
    except RuntimeError as e:
        # send_email_code 对配置错误、认证失败、地址异常等抛出可读中文说明
        logger.warning("发送重置密码验证码失败（可提示用户）: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 500
    except Exception as e:
        logger.exception("发送重置密码验证码失败: %s", e)
        return jsonify({'success': False, 'message': '验证码发送失败，请稍后再试。若持续失败请联系管理员。'}), 500
    finally:
        db.close()


@quickform_bp.route('/forgot_username', methods=['GET', 'POST'])
def forgot_username():
    """通过手机号或邮箱查询用户名（老师更容易记住邮箱）"""
    if request.method == 'POST':
        from flask import session
        raw = (request.form.get('account') or request.form.get('phone') or '').strip()
        if not raw:
            flash('请填写手机号或邮箱', 'danger')
            return redirect(url_for('quickform.forgot_username'))

        db = SessionLocal()
        try:
            # 安全策略：不在页面回显用户名，避免被枚举；仅当匹配到真实邮箱时发送邮件。
            now = time.time()
            cooldown_s = 60

            def _cooldown_key(k: str) -> str:
                return f'fu_last_{k}'

            # 邮箱查询：若存在则发送用户名到该邮箱；无论是否存在均提示同一句
            if '@' in raw:
                email = raw
                if not _USER_EMAIL_FORMAT_RE.match(email):
                    flash('邮箱格式不正确', 'danger')
                    return redirect(url_for('quickform.forgot_username'))
                if email.endswith('@noreply.local'):
                    # 占位邮箱无法收信：仍不暴露是否存在，仅提示使用手机号或联系管理员
                    flash('若您当时未绑定真实邮箱，请改用手机号找回，或联系管理员协助。', 'info')
                    return redirect(url_for('quickform.forgot_username'))

                last = float(session.get(_cooldown_key('email'), 0) or 0)
                if now - last < cooldown_s:
                    flash('操作过于频繁，请稍后再试（约1分钟）', 'warning')
                    return redirect(url_for('quickform.forgot_username'))
                session[_cooldown_key('email')] = now

                u = db.query(User).filter(User.email == email).first()
                if u and (u.email or '').strip() and (not _is_placeholder_or_empty_email(u.email)):
                    try:
                        send_username_reminder_email(email, u.username)
                    except Exception as e:
                        logger.warning("发送用户名提醒失败（邮箱=%s，已隐藏结果）: %s", email, e)
                flash('如果该邮箱对应账号存在，我们已发送用户名到您的邮箱（请注意查收垃圾箱）。', 'success')
            else:
                # 手机号查询：若存在且绑定了真实邮箱则发到绑定邮箱；无论是否存在均提示同一句
                phone = raw
                last = float(session.get(_cooldown_key('phone'), 0) or 0)
                if now - last < cooldown_s:
                    flash('操作过于频繁，请稍后再试（约1分钟）', 'warning')
                    return redirect(url_for('quickform.forgot_username'))
                session[_cooldown_key('phone')] = now

                users = db.query(User).filter(User.phone == phone).all()
                # 多账号同手机号的情况无法安全确认收件人，避免误发
                if len(users) == 1:
                    u = users[0]
                    target = (u.email or '').strip()
                    if target and (not _is_placeholder_or_empty_email(target)):
                        try:
                            send_username_reminder_email(target, u.username)
                        except Exception as e:
                            logger.warning("发送用户名提醒失败（phone=%s，已隐藏结果）: %s", phone, e)
                flash('如果该手机号对应账号存在且已绑定可收信邮箱，我们已发送用户名到绑定邮箱。', 'success')
        finally:
            db.close()

        return redirect(url_for('quickform.forgot_username'))

    return render_template('forgot_username.html')

@quickform_bp.route('/logout')
def logout():
    """登出：清除当前会话与「记住我」cookie，并删除旧版默认 cookie 名，避免串号"""
    logout_user()
    resp = make_response(redirect(url_for('quickform.login')))
    # 清除可能存在的旧版默认 cookie 名，避免部署新配置后仍用旧 cookie 恢复成他人会话
    for name in ('session', 'remember_token'):
        resp.set_cookie(name, '', max_age=0, path='/', samesite='Lax', secure=request.is_secure)
    return resp

@quickform_bp.route('/dashboard')
@login_required
def dashboard():
    """仪表盘"""
    db = SessionLocal()
    try:
        # 查询所有用户可以访问的任务
        # 1. 用户自己创建的任务
        own_tasks = (
            db.query(Task)
            .options(joinedload(Task.author), joinedload(Task.organization))
            .filter_by(user_id=current_user.id)
            .all()
        )
        
        # 2. 用户所在组织的任务
        user_orgs = db.query(OrganizationMember).filter_by(user_id=current_user.id).all()
        org_ids = [m.organization_id for m in user_orgs]
        org_tasks = (
            db.query(Task)
            .options(joinedload(Task.author), joinedload(Task.organization))
            .filter(Task.organization_id.in_(org_ids))
            .all()
        ) if org_ids else []
        
        # 3. 共享给用户的任务（带权限：只读/编辑）
        shared_records = db.query(TaskShare).filter_by(user_id=current_user.id).all()
        shared_task_ids = [s.task_id for s in shared_records]
        shared_tasks = (
            db.query(Task)
            .options(joinedload(Task.author), joinedload(Task.organization))
            .filter(Task.id.in_(shared_task_ids))
            .all()
        ) if shared_task_ids else []
        shared_can_edit = {s.task_id: s.can_edit for s in shared_records}
        
        # 合并并去重，并标记访问类型：owner / org / shared_edit / shared_readonly
        all_task_ids = set()
        tasks = []
        task_access = {}  # task_id -> 'owner' | 'org' | 'shared_edit' | 'shared_readonly'
        for task in own_tasks:
            if task.id not in all_task_ids:
                all_task_ids.add(task.id)
                tasks.append(task)
                task_access[task.id] = 'owner'
        for task in org_tasks:
            if task.id not in all_task_ids:
                all_task_ids.add(task.id)
                tasks.append(task)
                org_can_edit = _org_members_can_edit_tasks(db, task.organization_id)
                task_access[task.id] = 'org_edit' if org_can_edit else 'org_readonly'
        for task in shared_tasks:
            if task.id not in all_task_ids:
                all_task_ids.add(task.id)
                tasks.append(task)
                task_access[task.id] = 'shared_edit' if shared_can_edit.get(task.id, False) else 'shared_readonly'

        # 一次 SQL 聚合获取每个任务的提交数，避免模板里 task.submission|length 触发关系加载
        submission_count_map = {}
        if all_task_ids:
            rows = (
                db.query(Submission.task_id, func.count(Submission.id))
                .filter(Submission.task_id.in_(list(all_task_ids)))
                .group_by(Submission.task_id)
                .all()
            )
            submission_count_map = {task_id: int(cnt or 0) for task_id, cnt in rows}
        
        # 按创建时间倒序排序（我的任务 / 我的团队任务 独立呈现）
        own_tasks.sort(key=lambda t: t.created_at, reverse=True)
        org_tasks.sort(key=lambda t: t.created_at, reverse=True)
        shared_tasks.sort(key=lambda t: t.created_at, reverse=True)
        tasks = own_tasks + org_tasks + shared_tasks  # 兼容旧模板若有单列表
        
        user_record = db.get(User, current_user.id)
        task_count = len(own_tasks)  # 只统计自己创建的任务数
        task_limit = None
        is_certified = False
        if user_record:
            task_limit = user_record.task_limit
            is_certified = bool(user_record.is_certified)
        else:
            task_limit = getattr(current_user, 'task_limit', None)
            is_certified = bool(getattr(current_user, 'is_certified', False))
        # 含一键生成与「AI 继续修改」后台任务；去重（同一任务可能同时出现在我的任务与团队任务中）
        _pending_ids = set()
        pending_oneclick_tasks = []
        for _lst in (own_tasks, org_tasks, shared_tasks):
            for t in _lst:
                if t.id in _pending_ids:
                    continue
                if getattr(t, 'oneclick_generation_status', None) == 'pending':
                    _pending_ids.add(t.id)
                    pending_oneclick_tasks.append(t)
        pending_oneclick_tasks.sort(key=lambda x: x.id, reverse=True)
        return render_template(
            'dashboard.html',
            tasks=tasks,
            own_tasks=own_tasks,
            org_tasks=org_tasks,
            shared_tasks=shared_tasks,
            submission_count_map=submission_count_map,
            task_access=task_access,
            task_count=task_count,
            task_limit=task_limit,
            is_certified=is_certified,
            pending_oneclick_tasks=pending_oneclick_tasks,
            api_base_url=_public_site_base_url(),
        )
    finally:
        db.close()


def _run_oneclick_generation_background(task_id: int, user_id: int, full_prompt: str):
    """在后台线程中完成一键生成 HTML，避免同步阻塞触发 Nginx/网关超时。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.user_id != user_id:
            return
        if (getattr(task, 'oneclick_generation_status', None) or '') != 'pending':
            return
        ai_config = db.query(AIConfig).filter_by(user_id=user_id).first()
        decrypt_ai_config_inplace(ai_config)
        if not ai_config:
            ai_config = AIConfig(user_id=user_id, selected_model='chat_server')
        html_content = generate_html_page_from_prompt(full_prompt, call_ai_model, ai_config)
        os.makedirs(STATIC_UPLOADS, exist_ok=True)
        unique_filename = str(uuid.uuid4()) + '_oneclick.html'
        filepath = os.path.join(STATIC_UPLOADS, unique_filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        task.file_name = 'oneclick.html'
        task.file_path = filepath
        task.html_files = json.dumps([{'original_name': 'oneclick.html', 'saved_name': unique_filename}])
        task.ai_generated = True
        task.html_ai_edit_remaining = 3
        task.oneclick_generation_status = None
        task.oneclick_generation_error = None
        user_rec = db.get(User, user_id)
        if user_rec and (user_rec.is_admin() or getattr(user_rec, 'is_certified', False)):
            task.html_approved = 1
            task.html_approved_by = user_id
            task.html_approved_at = datetime.now()
        else:
            task.html_approved = 0
        db.commit()
        logger.info('一键生成后台完成 task_id=%s user_id=%s', task_id, user_id)
        try:
            analyze_html_file(task.id, user_id, filepath, SessionLocal, Task, AIConfig, read_file_content, call_ai_model)
        except Exception:
            logger.warning('一键生成完成后触发 HTML 分析调度失败 task_id=%s', task_id, exc_info=True)
    except Exception as e:
        db.rollback()
        logger.exception('一键生成后台失败 task_id=%s', task_id)
        try:
            task = db.get(Task, task_id)
            if task and task.user_id == user_id:
                task.oneclick_generation_status = 'failed'
                err = (str(e) or '').strip()
                task.oneclick_generation_error = (err[:4000] if err else '未知错误')
                task.file_path = None
                task.file_name = None
                task.html_files = None
                task.html_approved = 0
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


def _run_ai_revise_html_background(task_id: int, user_id: int, instructions: str, html_snapshot: str):
    """在后台线程中完成「AI 继续修改」HTML，避免同步阻塞触发 Nginx/网关超时。复用 oneclick_generation_status 表示进行中/失败。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or not _user_can_edit_task_row(db, task, user_id):
            return
        if (getattr(task, 'oneclick_generation_status', None) or '') != 'pending':
            return
        ai_config = db.query(AIConfig).filter_by(user_id=user_id).first()
        decrypt_ai_config_inplace(ai_config)
        if not ai_config:
            raise Exception('请先在个人中心配置 AI 后再使用「AI 继续修改」。')
        new_html = revise_html_with_ai(html_snapshot, instructions, call_ai_model, ai_config)
        os.makedirs(STATIC_UPLOADS, exist_ok=True)
        unique_filename = str(uuid.uuid4()) + '_revised.html'
        new_filepath = os.path.join(STATIC_UPLOADS, unique_filename)
        with open(new_filepath, 'w', encoding='utf-8') as f:
            f.write(new_html)
        old_path = task.file_path
        if old_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass
        task.file_path = new_filepath
        task.file_name = 'revised.html'
        task.html_files = json.dumps([{'original_name': 'revised.html', 'saved_name': unique_filename}])
        rem = getattr(task, 'html_ai_edit_remaining', None)
        if rem is not None and rem > 0:
            task.html_ai_edit_remaining = rem - 1
        user_rec = db.get(User, user_id)
        if user_rec and (user_rec.is_admin() or getattr(user_rec, 'is_certified', False)):
            task.html_approved = 1
            task.html_approved_by = user_id
            task.html_approved_at = datetime.now()
        else:
            task.html_approved = 0
        task.html_analysis = None
        task.oneclick_generation_status = None
        task.oneclick_generation_error = None
        db.commit()
        logger.info('AI 继续修改后台完成 task_id=%s user_id=%s', task_id, user_id)
    except Exception as e:
        db.rollback()
        logger.exception('AI 继续修改后台失败 task_id=%s', task_id)
        try:
            task = db.get(Task, task_id)
            if task and _user_can_edit_task_row(db, task, user_id):
                task.oneclick_generation_status = 'failed'
                err = (str(e) or '').strip()
                task.oneclick_generation_error = (err[:4000] if err else '未知错误')
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


# 一键生成追加说明默认值见 models.DEFAULT_ONECLICK_PROMPT_OPTIONS（数据库无记录时回退）
ONECLICK_PROMPT_OPTIONS = DEFAULT_ONECLICK_PROMPT_OPTIONS


@quickform_bp.route('/oneclick_create_task', methods=['GET', 'POST'])
@login_required
def oneclick_create_task():
    """一键生成新任务：登录用户可用，根据描述生成 HTML 并自动上传到新任务（需在个人中心配置 AI）。"""
    if request.method == 'GET':
        db = SessionLocal()
        try:
            prompt_options = _load_oneclick_prompt_tuples(db)
        finally:
            db.close()
        return render_template(
            'oneclick_create_task.html',
            prompt_options=prompt_options,
        )
    # POST
    title = (request.form.get('title') or '').strip()
    requirements = (request.form.get('requirements') or '').strip()
    if not title or not requirements:
        flash('请填写任务标题和具体页面需求。', 'danger')
        return redirect(url_for('quickform.oneclick_create_task'))
    db = SessionLocal()
    try:
        task_count = db.query(Task).filter_by(user_id=current_user.id).count()
        if not current_user.is_admin() and not current_user.can_create_task(SessionLocal, Task):
            flash('您已达到任务数量上限，无法创建新任务。', 'warning')
            return redirect(url_for('quickform.dashboard'))
        block = _email_requirement_block_for_next_task(db, current_user, task_count)
        if block == 'bind_email':
            flash('创建第二个及后续任务前请先在个人资料中绑定邮箱（修改为您的个人邮箱）。', 'warning')
            return redirect(url_for('quickform.profile', next=url_for('quickform.oneclick_create_task')))
        if block == 'verify_email':
            flash('创建第二个及后续任务前请先验证邮箱。', 'warning')
            return redirect(url_for('quickform.verify_email', next=url_for('quickform.oneclick_create_task')))
        # 创建新任务以得到 task_id 与 API 地址
        task = Task(title=title, description='', user_id=current_user.id, sharing_type='private')
        db.add(task)
        db.flush()
        new_task_pk = task.id  # 用于成功后跳转任务详情（避免 commit 后 session 状态影响）
        api_base = _public_site_base_url()
        api_url = f"{api_base}/api/{task.task_id}"
        # 拼接用户需求与勾选说明，作为发给 AI 的完整提示词（仅 full_prompt 用于生成，不入库）
        lines = [requirements]
        prompt_tuples = _load_oneclick_prompt_tuples(db)
        for key, _label, text in prompt_tuples:
            if request.form.get(key) == 'on':
                lines.append(text.replace('API地址', api_url))
        full_prompt = '\n\n'.join(lines)
        task.description = requirements  # 任务描述只存用户输入的那段，不包含勾选的预设说明
        # 先落库为「生成中」，在后台线程调用大模型，避免长时间占用 HTTP 请求导致 Nginx/网关超时
        task.ai_generated = True
        task.html_ai_edit_remaining = 3
        task.oneclick_generation_status = 'pending'
        task.oneclick_generation_error = None
        task.file_path = None
        task.file_name = None
        task.html_files = None
        task.html_approved = 0
        db.commit()
        uid = int(current_user.id)
        threading.Thread(
            target=_run_oneclick_generation_background,
            args=(new_task_pk, uid, full_prompt),
            daemon=True,
        ).start()
        return redirect(url_for('quickform.dashboard'))
    except Exception as e:
        db.rollback()
        logger.exception("一键创建任务失败")
        flash(MSG_GENERIC, 'danger')
        return redirect(url_for('quickform.oneclick_create_task'))
    finally:
        db.close()


@quickform_bp.route('/create_task', methods=['GET', 'POST'])
@login_required
def create_task():
    """创建任务"""
    db = SessionLocal()
    try:
        task_count = db.query(Task).filter_by(user_id=current_user.id).count()
        if not current_user.is_admin():
            if not current_user.can_create_task(SessionLocal, Task):
                task_limit = current_user.task_limit if current_user.task_limit != -1 else "无限制"
                flash(f'您已达到任务数量上限（{task_limit}个，当前{task_count}个）。如需创建更多任务，请在右上角个人中心申请教师认证', 'warning')
                return redirect(url_for('quickform.dashboard'))
            block = _email_requirement_block_for_next_task(db, current_user, task_count)
            if block == 'bind_email':
                flash('创建第二个及后续任务前请先在个人资料中绑定邮箱（修改为您的个人邮箱）。', 'warning')
                return redirect(url_for('quickform.profile', next=url_for('quickform.create_task')))
            if block == 'verify_email':
                flash('创建第二个及后续任务前请先验证邮箱。', 'warning')
                return redirect(url_for('quickform.verify_email', next=url_for('quickform.create_task')))
        
        if request.method == 'POST':
            title = request.form.get('title')
            description = request.form.get('description')
            
            task = Task(title=title, description=description, user_id=current_user.id)
            # 外链、教程与分享范围在「编辑任务」中设置，创建时固定为私有且无外链
            task.share_url = None
            task.tutorial_link = None
            task.sharing_type = 'private'
            task.organization_id = None
            
            # 优先检查Base64上传（用于公网环境）
            file_content_base64 = request.form.get('file_content_base64')
            file_name_base64 = request.form.get('file_name')
            
            if file_content_base64 and file_name_base64:
                # Base64上传方式
                try:
                    # 解码Base64
                    file_content = base64.b64decode(file_content_base64).decode('utf-8')
                    
                    # 验证文件扩展名
                    if not allowed_file(file_name_base64, ALLOWED_EXTENSIONS):
                        flash('文件上传失败或格式不支持，请重试。允许的格式：HTML/HTM，最大4MB。', 'danger')
                        return redirect(url_for('quickform.create_task'))
                    
                    # 保存文件（新文件存 static/uploads）
                    static_uploads = _static_uploads_dir()
                    unique_filename = str(uuid.uuid4()) + '_' + file_name_base64
                    filepath = os.path.join(static_uploads, unique_filename)
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(file_content)
                    
                    task.file_name = file_name_base64
                    task.file_path = filepath
                    task.html_files = json.dumps([{'original_name': file_name_base64, 'saved_name': unique_filename}])
                    
                    # 如果是HTML文件，设置审核状态
                    if filepath.lower().endswith(('.html', '.htm')):
                        if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                            task.html_approved = 1
                            task.html_approved_by = current_user.id
                            task.html_approved_at = datetime.now()
                            task.html_review_note = None
                        else:
                            task.html_approved = 0
                            task.html_approved_by = None
                            task.html_approved_at = None
                            task.html_review_note = None
                except Exception as e:
                    logger.error(f"Base64文件上传失败: {str(e)}", exc_info=True)
                    flash('文件上传失败，请重试。', 'danger')
                    return redirect(url_for('quickform.create_task'))
            else:
                # 传统文件上传方式（向后兼容，新文件存 static/uploads）
                file = request.files.get('file')
                if file and file.filename.strip():
                    unique_filename, filepath = save_uploaded_file(file, _static_uploads_dir())
                    if not unique_filename:
                        flash('文件上传失败或格式不支持，请重试。允许的格式：HTML/HTM，最大4MB。', 'danger')
                        return redirect(url_for('quickform.create_task'))
                    if filepath and filepath.lower().endswith(('.html', '.htm')) and os.path.getsize(filepath) > MAX_HTML_FILE_SIZE:
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        flash('单个 HTML 文件不得超过 4MB，请压缩后重试。', 'danger')
                        return redirect(url_for('quickform.create_task'))
                    task.file_name = file.filename
                    task.file_path = filepath
                    task.html_files = json.dumps([{'original_name': file.filename, 'saved_name': unique_filename}])
                    
                    # 如果是HTML文件，设置审核状态
                    if filepath and filepath.lower().endswith(('.html', '.htm')):
                        if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                            task.html_approved = 1
                            task.html_approved_by = current_user.id
                            task.html_approved_at = datetime.now()
                            task.html_review_note = None
                        else:
                            task.html_approved = 0
                            task.html_approved_by = None
                            task.html_approved_at = None
                            task.html_review_note = None
            
            db.add(task)
            db.commit()
            
            # 如果是HTML文件，在任务保存后自动在后台分析
            if task.file_path and task.file_path.lower().endswith(('.html', '.htm')):
                try:
                    analyze_html_file(task.id, current_user.id, task.file_path, SessionLocal, Task, AIConfig, read_file_content, call_ai_model)
                except Exception as e:
                    logger.error(f"启动HTML文件分析失败: {str(e)}", exc_info=True)
            
            flash('数据任务创建成功', 'success')
            onboard = (request.form.get('onboard') or '').strip()
            if onboard == '1':
                return redirect(url_for('quickform.task_detail', task_id=task.id, onboard=1, flow='quickStart_v1'))
            return redirect(url_for('quickform.task_detail', task_id=task.id))
        
        # GET 渲染创建页面
        user_record = db.get(User, current_user.id)
        task_count = db.query(Task).filter_by(user_id=current_user.id).count()
        task_limit = None
        is_certified = False
        if user_record:
            task_limit = user_record.task_limit
            is_certified = bool(user_record.is_certified)
        else:
            task_limit = getattr(current_user, 'task_limit', None)
            is_certified = bool(getattr(current_user, 'is_certified', False))
        
        return render_template('create_task.html', 
                             task_limit=task_limit, 
                             is_certified=is_certified, 
                             task_count=task_count)
    finally:
        db.close()

@quickform_bp.route('/task/<int:task_id>')
def task_detail(task_id):
    """任务详情（公开任务支持未登录访问，但不显示分析/导出）"""
    db = SessionLocal()
    try:
        task = (
            db.query(Task)
            .options(joinedload(Task.author), joinedload(Task.organization))
            .filter(Task.id == task_id)
            .first()
        )
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.index'))
        
        # 公开任务：任何人可查看（含未登录）
        if task.sharing_type == 'public':
            has_access = True
            can_analyze_export = False
            if current_user.is_authenticated:
                can_analyze_export = (
                    current_user.is_admin() or
                    task.user_id == current_user.id or
                    (task.organization_id and db.query(OrganizationMember).filter_by(
                        organization_id=task.organization_id,
                        user_id=current_user.id
                    ).first() is not None) or
                    db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first() is not None
                )
            user_liked = False
            if current_user.is_authenticated:
                user_liked = db.query(TaskLike).filter_by(
                    task_id=task.id, user_id=current_user.id
                ).first() is not None
        else:
            # 非公开任务：必须登录
            if not current_user.is_authenticated:
                flash('请先登录后查看', 'info')
                return redirect(url_for('quickform.login', next=url_for('quickform.task_detail', task_id=task_id)))
            has_access = False
            if current_user.is_admin() or task.user_id == current_user.id:
                has_access = True
            elif task.organization_id:
                is_org_member = db.query(OrganizationMember).filter_by(
                    organization_id=task.organization_id,
                    user_id=current_user.id
                ).first() is not None
                if is_org_member:
                    has_access = True
            else:
                is_shared = db.query(TaskShare).filter_by(
                    task_id=task.id,
                    user_id=current_user.id
                ).first() is not None
                if is_shared:
                    has_access = True
            if not has_access:
                flash('无权访问此任务', 'danger')
                return redirect(url_for('quickform.dashboard'))
            can_analyze_export = has_access
            user_liked = False
        
        # 任务详情页默认不加载提交数据，仅统计总数；具体数据在「查看数据」新页面中按需加载
        submission_count_query = db.query(Submission).filter_by(task_id=task.id)
        total_submissions = submission_count_query.count()
        submissions = []
        pagination = {'page': 1, 'per_page': 20, 'pages': 1}

        saved_filename = None
        try:
            if task.file_path:
                saved_filename = os.path.basename(task.file_path)
        except Exception:
            saved_filename = None

        # 多 HTML 文件列表（用于二维码与链接展示）
        html_files = []
        if task.html_files:
            try:
                html_files = json.loads(task.html_files)
            except Exception:
                html_files = []
        # 兼容旧数据：仅有单文件时也加入列表
        if task.file_name and saved_filename and not html_files:
            html_files = [{
                'original_name': task.file_name,
                'saved_name': saved_filename
            }]

        # 将「自动生成数据大屏」也作为一个 HTML 文件展示在任务详情里
        try:
            dash_status = getattr(task, 'dashboard_generation_status', None)
            dash_saved = getattr(task, 'dashboard_saved_name', None)
            if dash_status == 'completed' and dash_saved:
                already = any(isinstance(x, dict) and x.get('saved_name') == dash_saved for x in (html_files or []))
                if not already:
                    html_files.insert(0, {
                        'original_name': '数据大屏（自动生成）',
                        'saved_name': dash_saved,
                        'is_dashboard': True,
                    })
        except Exception:
            pass

        for f in html_files:
            if isinstance(f, dict) and 'saved_name' in f:
                if f.get('is_dashboard'):
                    _u = '/static/uploads/' + str(f['saved_name'])
                else:
                    _u = get_upload_file_url(f['saved_name'], task.file_path)
                f['url'] = _html_public_url_with_taskid(_u, getattr(task, 'task_id', None), f.get('saved_name'))

        # 任务详情页不展示分页列表，pagination 仅用于模板兼容（见上方已赋初值）
        
        # 仅登录用户需要组织列表与共享列表（用于分配任务/共享管理）
        user_organizations = []
        shared_users = []
        if current_user.is_authenticated:
            user_orgs_created = db.query(Organization).filter_by(creator_id=current_user.id).all()
            user_orgs_joined = db.query(OrganizationMember).filter_by(user_id=current_user.id).all()
            user_organizations = user_orgs_created + [m.organization for m in user_orgs_joined if m.organization.id not in [o.id for o in user_orgs_created]]
            shared_users = (
                db.query(TaskShare)
                .options(joinedload(TaskShare.user))
                .filter_by(task_id=task.id)
                .all()
            )

        # 公开项目且当前用户非所有者/管理员等：仅展示任务名称、简介、网页（不展示数据与导出等）
        is_public_visitor = (task.sharing_type == 'public' and not can_analyze_export)

        # 当前用户是否为组织创建者或组织管理员（用于显示组织成员权限开关）
        can_edit_org_settings = False
        if task.organization_id and current_user.is_authenticated:
            org_creator_id = _org_creator_id(db, task.organization_id)
            if org_creator_id == current_user.id:
                can_edit_org_settings = True
            else:
                om = db.query(OrganizationMember).filter_by(
                    organization_id=task.organization_id, user_id=current_user.id
                ).first()
                if om and om.role == 'admin':
                    can_edit_org_settings = True

        # 是否可编辑（仅所有者/管理员/组织成员且组织开启编辑时/被共享且权限为编辑）：只读用户可查看与导出，不可编辑任务与删除数据
        can_edit_task = False
        if current_user.is_authenticated:
            if current_user.is_admin() or task.user_id == current_user.id:
                can_edit_task = True
            elif task.organization_id:
                org_mem = db.query(OrganizationMember).filter_by(
                    organization_id=task.organization_id, user_id=current_user.id
                ).first()
                if org_mem and _org_members_can_edit_tasks(db, task.organization_id):
                    can_edit_task = True
            else:
                share_record = db.query(TaskShare).filter_by(
                    task_id=task.id, user_id=current_user.id
                ).first()
                if share_record and share_record.can_edit:
                    can_edit_task = True

        quota_ui = None
        can_request_quota_relief = False
        if can_analyze_export and current_user.is_authenticated:
            gct = int(getattr(task, "api_task_get_count", None) or 0)
            act = int(getattr(task, "api_task_all_count", None) or 0)
            abt = int(getattr(task, "api_task_all_bytes_total", None) or 0)
            base_r, base_b = _get_site_all_quota_defaults(db)
            max_r = base_r + int(getattr(task, "quota_extra_all_reads", None) or 0)
            max_b = base_b + int(getattr(task, "quota_extra_all_bytes", None) or 0)
            pend = db.query(TaskQuotaRequest).filter_by(task_id=task.id, status=0).first()
            quota_ui = {
                "get_count": gct,
                "all_count": act,
                "all_bytes": abt,
                "max_reads": max_r,
                "max_bytes": max_b,
                "total_get": gct + act,
                "pending": pend,
            }
            if current_user.is_admin() or task.user_id == current_user.id or can_edit_task:
                can_request_quota_relief = True

        task_author_name = db.query(User.username).filter(User.id == task.user_id).scalar() or ''

        return render_template(
            'task_detail.html',
            task=task,
            task_author_name=task_author_name,
            submissions=submissions,
            total_submissions=total_submissions,
            pagination=pagination,
            saved_filename=saved_filename,
            html_files=html_files,
            user_organizations=user_organizations,
            shared_users=shared_users,
            can_analyze_export=can_analyze_export,
            can_edit_task=can_edit_task,
            can_edit_org_settings=can_edit_org_settings,
            user_liked=user_liked,
            is_public_visitor=is_public_visitor,
            quota_ui=quota_ui,
            can_request_quota_relief=can_request_quota_relief,
            api_base_url=_public_site_base_url(),
        )
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/submission_manage_code/generate', methods=['POST'])
@login_required
def generate_submission_manage_code(task_id):
    """为任务生成或重置删改认证码。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        share_rec = db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first()
        org_mem = db.query(OrganizationMember).filter_by(
            organization_id=task.organization_id, user_id=current_user.id
        ).first() if task.organization_id else None
        can_edit = (
            current_user.is_admin() or task.user_id == current_user.id or
            (org_mem and _org_members_can_edit_tasks(db, task.organization_id)) or
            (share_rec and share_rec.can_edit)
        )
        if not can_edit:
            flash('无权为该任务生成删改认证码', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task.id))
        task.submission_manage_code = _generate_submission_manage_code()
        db.commit()
        flash('已生成新的删改认证码。请妥善保存，后续可用于带码删改提交数据。', 'success')
        return redirect(url_for('quickform.task_detail', task_id=task.id, show_manage_code_tip=1))
    except Exception as e:
        db.rollback()
        logger.exception('generate_submission_manage_code failed: %s', e)
        flash('生成删改认证码失败，请稍后重试。', 'danger')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/submission_manage_code/disable', methods=['POST'])
@login_required
def disable_submission_manage_code(task_id):
    """关闭/停用任务删改认证码（清空）。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        share_rec = db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first()
        org_mem = db.query(OrganizationMember).filter_by(
            organization_id=task.organization_id, user_id=current_user.id
        ).first() if task.organization_id else None
        can_edit = (
            current_user.is_admin() or task.user_id == current_user.id or
            (org_mem and _org_members_can_edit_tasks(db, task.organization_id)) or
            (share_rec and share_rec.can_edit)
        )
        if not can_edit:
            flash('无权关闭该任务的删改认证码', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task.id))
        task.submission_manage_code = None
        db.commit()
        flash('已关闭删改认证码。后续带码删改请求将不再允许。', 'success')
        return redirect(url_for('quickform.task_detail', task_id=task.id))
    except Exception as e:
        db.rollback()
        logger.exception('disable_submission_manage_code failed: %s', e)
        flash('关闭删改认证码失败，请稍后重试。', 'danger')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/quota_request', methods=['POST'])
@login_required
def task_quota_request_submit(task_id):
    """任务所有者/可编辑协作者/管理员：申请提高 /all 接口额度"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        allowed = current_user.is_admin() or task.user_id == current_user.id
        if not allowed and task.organization_id:
            om = db.query(OrganizationMember).filter_by(
                organization_id=task.organization_id, user_id=current_user.id
            ).first()
            if om and _org_members_can_edit_tasks(db, task.organization_id):
                allowed = True
        if not allowed:
            shr = db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first()
            if shr and shr.can_edit:
                allowed = True
        if not allowed:
            flash('无权为该任务提交加额申请', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        if db.query(TaskQuotaRequest).filter_by(task_id=task.id, status=0).first():
            flash('该任务已有待审核的加额申请，请等待管理员处理', 'info')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        note = (request.form.get('applicant_note') or '').strip()
        req_reads_raw = (request.form.get('requested_extra_reads') or '0').strip()
        req_mb_raw = (request.form.get('requested_extra_mb') or '0').strip()
        try:
            req_reads = max(0, int(req_reads_raw or 0))
            req_mb = max(0, int(req_mb_raw or 0))
        except ValueError:
            flash('申请次数和流量请输入非负整数', 'warning')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        if req_reads == 0 and req_mb == 0:
            flash('请至少填写一项申请额度（次数或流量）', 'warning')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        if len(note) < 2:
            flash('请填写申请理由（至少 2 个字）', 'warning')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        req = TaskQuotaRequest(
            task_id=task.id,
            user_id=current_user.id,
            applicant_note=note,
            requested_extra_reads=req_reads,
            requested_extra_mb=req_mb,
            status=0,
        )
        db.add(req)

        row = db.get(SiteQuotaDefault, 1)
        auto_enabled = bool(int(getattr(row, 'auto_quota_approve_enabled', 0) or 0)) if row else False
        auto_max_reads = int(getattr(row, 'auto_quota_approve_max_reads', 0) or 0) if row else 0
        auto_max_mb = int(getattr(row, 'auto_quota_approve_max_mb', 0) or 0) if row else 0
        auto_ok = auto_enabled and req_reads <= auto_max_reads and req_mb <= auto_max_mb

        if auto_ok:
            task.quota_extra_all_reads = (task.quota_extra_all_reads or 0) + req_reads
            task.quota_extra_all_bytes = int(task.quota_extra_all_bytes or 0) + req_mb * 1024 * 1024
            req.status = 1
            req.reviewed_at = datetime.now()
            req.reviewed_by = None
            req.granted_extra_reads = req_reads
            req.granted_extra_mb = req_mb
            req.review_note = f"系统自动审批：阈值内（次数≤{auto_max_reads}，流量≤{auto_max_mb}MB）"
            db.commit()
            flash(f'申请已自动通过：+{req_reads} 次 /all、+{req_mb} MB', 'success')
        else:
            db.commit()
            flash('已提交加额申请，管理员审核通过后将增加相应次数与流量额度', 'success')
    except Exception as e:
        db.rollback()
        logger.exception("提交 task quota 申请失败: %s", e)
        flash('提交失败，请稍后重试', 'danger')
    finally:
        db.close()
    return redirect(url_for('quickform.task_detail', task_id=task_id))


@quickform_bp.route('/task/<int:task_id>/data')
def task_data_view(task_id):
    """提交数据查看页：在新页面中展示具体数据，定时刷新；仅对有分析/导出权限的用户开放"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        can_analyze_export = False
        if task.sharing_type == 'public':
            if current_user.is_authenticated:
                can_analyze_export = (
                    current_user.is_admin() or task.user_id == current_user.id or
                    (task.organization_id and db.query(OrganizationMember).filter_by(
                        organization_id=task.organization_id, user_id=current_user.id
                    ).first() is not None) or
                    db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first() is not None
                )
        else:
            if not current_user.is_authenticated:
                return redirect(url_for('quickform.login', next=url_for('quickform.task_data_view', task_id=task_id)))
            if current_user.is_admin() or task.user_id == current_user.id:
                can_analyze_export = True
            elif task.organization_id:
                if db.query(OrganizationMember).filter_by(
                    organization_id=task.organization_id, user_id=current_user.id
                ).first() is not None:
                    can_analyze_export = True
            else:
                if db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first() is not None:
                    can_analyze_export = True
        if not can_analyze_export:
            flash('无权查看该任务的提交数据', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        # 是否可编辑/删除数据（仅所有者、管理员、组织成员且组织开启编辑、被共享且编辑权限）
        can_edit_task = False
        if current_user.is_admin() or task.user_id == current_user.id:
            can_edit_task = True
        elif task.organization_id:
            org_mem = db.query(OrganizationMember).filter_by(
                organization_id=task.organization_id, user_id=current_user.id
            ).first()
            org_edit_enabled = _org_members_can_edit_tasks(db, task.organization_id)
            if org_mem and org_edit_enabled:
                can_edit_task = True
        else:
            share_record = db.query(TaskShare).filter_by(
                task_id=task.id, user_id=current_user.id
            ).first()
            if share_record and share_record.can_edit:
                can_edit_task = True
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        search_q = (request.args.get('search') or request.args.get('q') or '').strip()
        page = max(1, page)
        per_page = min(max(1, per_page), 200)
        submission_query = (
            db.query(Submission)
            .filter_by(task_id=task.id)
            .order_by(Submission.submitted_at.desc())
        )
        if search_q:
            # LIKE 通配符转义：% _ \ -> \% \_ \\
            like_esc = search_q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            submission_query = submission_query.filter(Submission.data.like('%' + like_esc + '%'))
        total_submissions = None
        use_cache = not search_q
        now_ts = time.time()
        dynamic_ttl = _get_dynamic_task_data_ttl(task.id, now_ts) if use_cache else 0
        if use_cache:
            with _task_data_cache_lock:
                rec = _task_data_count_cache.get(task.id)
                if rec and rec['expire_at'] > now_ts:
                    total_submissions = rec['total']
        if total_submissions is None:
            total_submissions = submission_query.count()
            if use_cache:
                with _task_data_cache_lock:
                    _task_data_count_cache[task.id] = {'total': total_submissions, 'expire_at': now_ts + dynamic_ttl}
        total_pages = max(math.ceil(total_submissions / per_page), 1) if total_submissions else 1
        if page > total_pages:
            page = total_pages
        submissions = None
        if use_cache:
            page_key = f"{task.id}:{page}:{per_page}"
            cached_ids = None
            with _task_data_cache_lock:
                rec = _task_data_page_ids_cache.get(page_key)
                if rec and rec['expire_at'] > now_ts:
                    cached_ids = rec['ids']
            if cached_ids is not None:
                by_id = {s.id: s for s in db.query(Submission).filter(Submission.id.in_(cached_ids)).all()}
                submissions = [by_id[sid] for sid in cached_ids if sid in by_id]
            if submissions is None:
                submissions = (
                    submission_query
                    .offset((page - 1) * per_page)
                    .limit(per_page)
                    .all()
                )
                with _task_data_cache_lock:
                    _task_data_page_ids_cache[page_key] = {
                        'ids': [s.id for s in submissions],
                        'expire_at': now_ts + dynamic_ttl,
                    }
        else:
            submissions = (
                submission_query
                .offset((page - 1) * per_page)
                .limit(per_page)
                .all()
            )
        pagination = {'page': page, 'per_page': per_page, 'pages': total_pages}
        return render_template(
            'task_data_view.html',
            task=task,
            submissions=submissions,
            total_submissions=total_submissions,
            pagination=pagination,
            can_edit_task=can_edit_task,
            search_q=search_q,
            api_base_url=_public_site_base_url(),
        )
    finally:
        db.close()


@quickform_bp.route('/edit_task/<int:task_id>', methods=['GET', 'POST'])
@login_required
def edit_task(task_id):
    """编辑任务"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        # 权限检查：管理员、任务所有者、组织成员且组织开启编辑、被共享且权限为「编辑」者可编辑
        has_edit_permission = False
        if current_user.is_admin() or task.user_id == current_user.id:
            has_edit_permission = True
        elif task.organization_id:
            org_mem = db.query(OrganizationMember).filter_by(
                organization_id=task.organization_id,
                user_id=current_user.id
            ).first()
            if org_mem and _org_members_can_edit_tasks(db, task.organization_id):
                has_edit_permission = True
        else:
            share_record = db.query(TaskShare).filter_by(
                task_id=task.id,
                user_id=current_user.id
            ).first()
            if share_record and share_record.can_edit:
                has_edit_permission = True
        
        if not has_edit_permission:
            flash('无权编辑此任务', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        if request.method == 'POST':
            # 一键生成任务：AI 继续修改（在现有 HTML 基础上按说明修订）
            if request.form.get('action') == 'ai_revise_html':
                instructions = (request.form.get('revision_instructions') or '').strip()
                if not instructions:
                    flash('请填写修改说明。', 'warning')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                if not getattr(task, 'ai_generated', False) or getattr(task, 'html_ai_edit_remaining', None) is None or task.html_ai_edit_remaining <= 0:
                    flash('该任务不支持 AI 继续修改或修改次数已用完。', 'warning')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                current_html_path = None
                if task.file_path and os.path.exists(task.file_path):
                    current_html_path = task.file_path
                if not current_html_path and task.html_files:
                    try:
                        files_list = json.loads(task.html_files)
                        if files_list:
                            first_saved = files_list[0].get('saved_name')
                            if first_saved:
                                current_html_path = os.path.join(UPLOAD_FOLDER, first_saved)
                                if not os.path.exists(current_html_path):
                                    current_html_path = None
                    except Exception:
                        pass
                if not current_html_path:
                    flash('未找到当前 HTML 文件，无法继续修改。', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                current_html = read_file_content(current_html_path)
                if not current_html or '<' not in current_html:
                    flash('当前 HTML 内容无效，无法继续修改。', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                ai_config = db.query(AIConfig).filter_by(user_id=current_user.id).first()
                decrypt_ai_config_inplace(ai_config)
                if not ai_config:
                    flash('请先在个人中心配置 AI 后再使用「AI 继续修改」。', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                if (getattr(task, 'oneclick_generation_status', None) or '') == 'pending':
                    flash('该任务的 AI 页面正在后台处理中，请稍后在「数据任务」查看进度后再试。', 'warning')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                # 与一键生成相同：后台执行模型调用，立即跳转，避免网关超时
                task.oneclick_generation_status = 'pending'
                task.oneclick_generation_error = None
                db.commit()
                uid = int(current_user.id)
                threading.Thread(
                    target=_run_ai_revise_html_background,
                    args=(task.id, uid, instructions, current_html),
                    daemon=True,
                ).start()
                return redirect(url_for('quickform.dashboard'))

            # 替换某个已上传 HTML 文件，但保留 URL（saved_name 不变）
            if request.form.get('action') == 'replace_html_keep_url':
                target_saved = (request.form.get('replace_saved_name') or '').strip()
                f = request.files.get('replace_file')
                if not target_saved or ('/' in target_saved) or ('\\' in target_saved) or ('..' in target_saved):
                    flash('替换失败：目标文件名无效。', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                if not f or not getattr(f, 'filename', None) or not (f.filename or '').strip():
                    flash('请选择要替换上传的 HTML 文件。', 'warning')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                if not allowed_file(f.filename, ALLOWED_EXTENSIONS):
                    flash('文件格式不支持：仅允许 HTML/HTM。', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))

                # 必须存在于任务的 html_files 列表中
                existing_files = []
                try:
                    existing_files = json.loads(task.html_files) if task.html_files else []
                except Exception:
                    existing_files = []
                if not isinstance(existing_files, list):
                    existing_files = []
                idx = None
                for i, rec in enumerate(existing_files):
                    if isinstance(rec, dict) and (rec.get('saved_name') or '').strip() == target_saved:
                        idx = i
                        break
                if idx is None:
                    flash('替换失败：该文件不属于本任务，或记录已丢失。', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))

                # 找到旧文件所在目录（static/uploads 优先，其次旧 UPLOAD_FOLDER），否则写入 static/uploads
                static_uploads = _static_uploads_dir()
                target_path = None
                for folder in (static_uploads, UPLOAD_FOLDER):
                    p = os.path.join(folder, target_saved)
                    if os.path.exists(p):
                        target_path = p
                        break
                if not target_path:
                    target_path = os.path.join(static_uploads, target_saved)

                # 保存：覆盖写入同名文件，从而 URL 不变
                try:
                    # 先写到临时文件，避免中途失败导致原文件被破坏
                    tmp_saved, tmp_path = save_uploaded_file(f, static_uploads)
                    if not tmp_saved or not tmp_path:
                        flash('文件上传失败，请重试。允许的格式：HTML/HTM，最大 4MB。', 'danger')
                        return redirect(url_for('quickform.edit_task', task_id=task.id))
                    if tmp_path and os.path.getsize(tmp_path) > MAX_HTML_FILE_SIZE:
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                        flash('单个 HTML 文件不得超过 4MB，请压缩后重试。', 'danger')
                        return redirect(url_for('quickform.edit_task', task_id=task.id))
                    # 覆盖目标
                    os.replace(tmp_path, target_path)
                    # 清理 tmp_saved 变量指向的文件名（os.replace 已把 tmp_path 移走；若目标路径不同盘符可能失败，但这里同目录）
                except Exception as e:
                    logger.error("替换保留URL失败: %s", str(e), exc_info=True)
                    flash('替换失败，请重试。', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))

                # 更新 DB：原始名更新；如果替换的是第一个文件，同步 file_name/file_path
                existing_files[idx]['original_name'] = f.filename
                task.html_files = json.dumps(existing_files) if existing_files else None
                if idx == 0:
                    task.file_name = f.filename
                    task.file_path = target_path

                # 审核/分析与普通上传保持一致
                if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                    task.html_approved = 1
                    task.html_approved_by = current_user.id
                    task.html_approved_at = datetime.now()
                    task.html_review_note = None
                else:
                    task.html_approved = 0
                    task.html_approved_by = None
                    task.html_approved_at = None
                    task.html_review_note = None
                task.html_analysis = None
                try:
                    analyze_html_file(task.id, current_user.id, target_path, SessionLocal, Task, AIConfig, read_file_content, call_ai_model)
                except Exception as e2:
                    logger.error("启动HTML文件分析失败(替换保留URL): %s", str(e2), exc_info=True)

                db.commit()
                flash('已替换文件，URL 保持不变。', 'success')
                return redirect(url_for('quickform.edit_task', task_id=task.id))

            title = request.form.get('title')
            description = request.form.get('description')
            remove_file = request.form.get('remove_file')
            files_to_remove = request.form.get('files_to_remove')
            html_files_data = request.form.get('html_files_data')
            file_content_base64 = request.form.get('file_content_base64')
            file_name_base64 = request.form.get('file_name')
            file_upload = request.files.get('file')
            files_multipart = request.files.getlist('files')  # 编辑页直接 multipart 多文件上传
            if files_multipart:
                try:
                    flist = [f for f in files_multipart if f and f.filename and (f.filename or '').strip()]
                except Exception:
                    flist = []
            else:
                flist = []

            task.title = title
            task.description = description
            task.share_url = (request.form.get('share_url') or '').strip() or None
            task.tutorial_link = (request.form.get('tutorial_link') or '').strip() or None
            
            # 处理文件删除（先执行，再处理上传）
            if files_to_remove:
                try:
                    remove_list = json.loads(files_to_remove)
                    existing_files = json.loads(task.html_files) if task.html_files else []
                    if not isinstance(existing_files, list):
                        existing_files = []
                    
                    for saved_name in remove_list:
                        # 删除文件（可能在 static/uploads 或旧 core/uploads）
                        for folder in (_static_uploads_dir(), UPLOAD_FOLDER):
                            filepath = os.path.join(folder, saved_name)
                            if os.path.exists(filepath):
                                try:
                                    os.remove(filepath)
                                except OSError:
                                    pass
                                break
                        # 从列表中移除（兼容 saved_name 或 path 等字段）
                        sn = str(saved_name).strip()
                        existing_files = [f for f in existing_files if isinstance(f, dict) and f.get('saved_name') != sn]
                    
                    task.html_files = json.dumps(existing_files) if existing_files else None
                    # 同步单文件字段：任务详情页会优先用 html_files，为空时回退到 file_name/file_path，必须一致避免删除了仍显示
                    if not existing_files:
                        task.file_name = None
                        task.file_path = None
                        task.html_review_note = None
                    else:
                        first = existing_files[0]
                        task.file_name = first.get('original_name') or first.get('saved_name')
                        static_uploads = _static_uploads_dir()
                        task.file_path = os.path.join(static_uploads, first.get('saved_name', ''))
                except Exception as e:
                    logger.error(f"文件删除失败: {str(e)}")
            
            # 编辑页直接 multipart 多文件上传（不走 Base64，更简洁）
            if flist:
                try:
                    existing_files = json.loads(task.html_files) if task.html_files else []
                    if len(existing_files) + len(flist) > 10:
                        flash(f'最多只能上传 10 个 HTML 文件，当前已有 {len(existing_files)} 个。', 'danger')
                        return redirect(url_for('quickform.edit_task', task_id=task.id))
                    static_uploads = _static_uploads_dir()
                    for f in flist:
                        unique_filename, filepath = save_uploaded_file(f, static_uploads)
                        if not unique_filename:
                            flash('文件上传失败或格式不支持，请重试。允许的格式：HTML/HTM，最大 4MB。', 'danger')
                            return redirect(url_for('quickform.edit_task', task_id=task.id))
                        if filepath and os.path.getsize(filepath) > MAX_HTML_FILE_SIZE:
                            try:
                                os.remove(filepath)
                            except OSError:
                                pass
                            flash('单个 HTML 文件不得超过 4MB，请压缩后重试。', 'danger')
                            return redirect(url_for('quickform.edit_task', task_id=task.id))
                        existing_files.append({'original_name': f.filename, 'saved_name': unique_filename})
                    task.html_files = json.dumps(existing_files)
                    if not task.file_path and existing_files:
                        first_saved = existing_files[0]['saved_name']
                        task.file_path = os.path.join(static_uploads, first_saved)
                        task.file_name = existing_files[0]['original_name']
                    if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                        task.html_approved = 1
                        task.html_approved_by = current_user.id
                        task.html_approved_at = datetime.now()
                    else:
                        task.html_approved = 0
                    task.html_analysis = None
                    if existing_files:
                        first_path = os.path.join(static_uploads, existing_files[-len(flist)]['saved_name'])
                        if os.path.exists(first_path):
                            try:
                                analyze_html_file(task.id, current_user.id, first_path, SessionLocal, Task, AIConfig, read_file_content, call_ai_model)
                            except Exception as e2:
                                logger.error("启动HTML文件分析失败(编辑): %s", str(e2), exc_info=True)
                except Exception as e:
                    logger.error("multipart 多文件上传失败: %s", str(e), exc_info=True)
                    flash('文件上传失败，请重试。', 'danger')
            # 回调/API：Base64 多文件（html_files_data），保留供重制 HTML 等场景
            elif html_files_data:
                try:
                    new_files = json.loads(html_files_data)
                    existing_files = json.loads(task.html_files) if task.html_files else []
                    if len(existing_files) + len(new_files) > 10:
                        flash(f'最多只能上传10个HTML文件！当前已有{len(existing_files)}个，尝试上传{len(new_files)}个', 'danger')
                        return redirect(url_for('quickform.edit_task', task_id=task.id))
                    static_uploads = _static_uploads_dir()
                    for file_data in new_files:
                        file_name = file_data.get('name') or file_data.get('original_name', '')
                        file_content = base64.b64decode(file_data['content']).decode('utf-8')
                        unique_filename = str(uuid.uuid4()) + '_' + (file_name or 'index.html')
                        filepath = os.path.join(static_uploads, unique_filename)
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(file_content)
                        existing_files.append({'original_name': file_name, 'saved_name': unique_filename})
                    task.html_files = json.dumps(existing_files)
                    if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                        task.html_approved = 1
                        task.html_approved_by = current_user.id
                        task.html_approved_at = datetime.now()
                    else:
                        task.html_approved = 0
                except Exception as e:
                    logger.error(f"多文件上传(html_files_data)失败: {str(e)}")
                    flash('文件上传失败', 'danger')
            # 优先检查Base64单文件（用于回调/公网，重制 HTML）
            elif file_content_base64 and file_name_base64:
                # Base64上传方式
                try:
                    # 解码Base64
                    file_content = base64.b64decode(file_content_base64).decode('utf-8')
                    
                    # 验证文件扩展名
                    if not allowed_file(file_name_base64, ALLOWED_EXTENSIONS):
                        flash('文件上传失败或格式不支持，请重试。允许的格式：HTML/HTM，最大4MB。', 'danger')
                        return redirect(url_for('quickform.edit_task', task_id=task.id))
                    
                    # 删除旧文件
                    if task.file_path and os.path.exists(task.file_path):
                        os.remove(task.file_path)
                    
                    # 保存文件到 static/uploads（与 create_task 一致）
                    static_uploads = _static_uploads_dir()
                    unique_filename = str(uuid.uuid4()) + '_' + file_name_base64
                    filepath = os.path.join(static_uploads, unique_filename)
                    
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(file_content)
                    
                    task.file_name = file_name_base64
                    task.file_path = filepath
                    task.html_files = json.dumps([{'original_name': file_name_base64, 'saved_name': unique_filename}])
                    
                    # 如果是HTML文件，设置审核状态
                    if filepath.lower().endswith(('.html', '.htm')):
                        if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                            task.html_approved = 1
                            task.html_approved_by = current_user.id
                            task.html_approved_at = datetime.now()
                            task.html_review_note = None
                        else:
                            task.html_approved = 0
                            task.html_approved_by = None
                            task.html_approved_at = None
                            task.html_review_note = None
                        task.html_analysis = None  # 清空旧的分析结果
                        
                        # 后台分析（不影响上传成功）
                        try:
                            analyze_html_file(task.id, current_user.id, filepath, SessionLocal, Task, AIConfig, read_file_content, call_ai_model)
                        except Exception as e:
                            logger.error(f"启动HTML文件分析失败(编辑): {str(e)}", exc_info=True)
                except Exception as e:
                    logger.error(f"Base64文件上传失败: {str(e)}", exc_info=True)
                    flash('文件上传失败，请重试。', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
            else:
                # 传统文件上传方式（向后兼容，新文件存 static/uploads）
                file = file_upload
                if file and file.filename and (file.filename or '').strip():
                    unique_filename, filepath = save_uploaded_file(file, _static_uploads_dir())
                    if not unique_filename:
                        flash('文件上传失败或格式不支持，请重试。允许的格式：HTML/HTM，最大4MB。', 'danger')
                        return redirect(url_for('quickform.edit_task', task_id=task.id))
                    if filepath and filepath.lower().endswith(('.html', '.htm')) and os.path.getsize(filepath) > MAX_HTML_FILE_SIZE:
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                        flash('单个 HTML 文件不得超过 4MB，请压缩后重试。', 'danger')
                        return redirect(url_for('quickform.edit_task', task_id=task.id))
                    # 删除旧文件
                    if task.file_path and os.path.exists(task.file_path):
                        os.remove(task.file_path)
                    
                    task.file_name = file.filename
                    task.file_path = filepath
                    task.html_files = json.dumps([{'original_name': file.filename, 'saved_name': unique_filename}])
                    
                    # 如果是HTML文件，设置审核状态
                    if filepath.lower().endswith(('.html', '.htm')):
                        if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                            task.html_approved = 1
                            task.html_approved_by = current_user.id
                            task.html_approved_at = datetime.now()
                            task.html_review_note = None
                        else:
                            task.html_approved = 0
                            task.html_approved_by = None
                            task.html_approved_at = None
                            task.html_review_note = None
                        task.html_analysis = None  # 清空旧的分析结果
                        
                        # 后台分析（不影响上传成功）
                        try:
                            analyze_html_file(task.id, current_user.id, filepath, SessionLocal, Task, AIConfig, read_file_content, call_ai_model)
                        except Exception as e:
                            logger.error(f"启动HTML文件分析失败(编辑): {str(e)}", exc_info=True)
            if remove_file:
                if task.file_path and os.path.exists(task.file_path):
                    os.remove(task.file_path)
                task.file_name = None
                task.file_path = None
                task.html_files = None
                task.html_review_note = None
            
            # 分享范围：仅任务所有者或管理员可在编辑页修改（与创建页拆分的字段一致）
            if current_user.is_admin() or task.user_id == current_user.id:
                share_scope = (request.form.get('share_scope') or 'private').strip()
                organization_id = request.form.get('organization_id')
                if share_scope == 'public':
                    if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                        task.sharing_type = 'public'
                        task.organization_id = None
                        task.public_approved = 0
                    else:
                        flash('只有通过教师认证的用户才能公开项目到共享区', 'warning')
                        task.sharing_type = 'organization' if task.organization_id else 'private'
                        task.public_approved = 0
                elif share_scope == 'organization' and organization_id and str(organization_id).strip() and str(organization_id).strip() != 'none':
                    try:
                        org_id = int(organization_id)
                        is_member = db.query(OrganizationMember).filter_by(
                            organization_id=org_id,
                            user_id=current_user.id
                        ).first() is not None
                        org = db.get(Organization, org_id)
                        if org and (is_member or org.creator_id == current_user.id):
                            task.organization_id = org_id
                            task.sharing_type = 'organization'
                            task.public_approved = 0
                        else:
                            flash('无权将该任务关联到所选组织', 'warning')
                    except (ValueError, TypeError):
                        pass
                else:
                    task.organization_id = None
                    task.sharing_type = 'private'
                    task.public_approved = 0
            
            db.commit()
            
            flash('任务更新成功', 'success')
            return redirect(url_for('quickform.task_detail', task_id=task.id))
        
        saved_filename = None
        try:
            if task.file_path:
                saved_filename = os.path.basename(task.file_path)
        except Exception:
            saved_filename = None
        
        # 解析多HTML文件列表
        html_files = []
        if task.html_files:
            try:
                html_files = json.loads(task.html_files)
            except:
                html_files = []
        
        # 如果有旧的单文件，转换为新格式
        if task.file_name and task.file_path and not html_files:
            html_files = [{
                'original_name': task.file_name,
                'saved_name': os.path.basename(task.file_path)
            }]
        for f in html_files:
            if isinstance(f, dict) and 'saved_name' in f:
                _u = get_upload_file_url(f['saved_name'], task.file_path)
                f['url'] = _html_public_url_with_taskid(_u, getattr(task, 'task_id', None), f.get('saved_name'))
        
        task_ai_generated = getattr(task, 'ai_generated', False)
        task_html_ai_edit_remaining = getattr(task, 'html_ai_edit_remaining', None)
        task_html_ai_async_pending = (getattr(task, 'oneclick_generation_status', None) or '') == 'pending'
        user_organizations = []
        if current_user.is_admin() or task.user_id == current_user.id:
            user_orgs_created = db.query(Organization).filter_by(creator_id=current_user.id).all()
            user_orgs_joined = db.query(OrganizationMember).filter_by(user_id=current_user.id).all()
            seen = {o.id for o in user_orgs_created}
            user_organizations = list(user_orgs_created)
            for m in user_orgs_joined:
                if m.organization and m.organization.id not in seen:
                    seen.add(m.organization.id)
                    user_organizations.append(m.organization)
        return render_template(
            'edit_task.html',
            task=task,
            saved_filename=saved_filename,
            html_files=html_files,
            task_ai_generated=task_ai_generated,
            task_html_ai_edit_remaining=task_html_ai_edit_remaining,
            task_html_ai_async_pending=task_html_ai_async_pending,
            user_organizations=user_organizations,
            api_base_url=_public_site_base_url(),
        )
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/visibility', methods=['POST'])
@login_required
def set_task_visibility(task_id):
    """在任务详情页修改任务的公开范围（私有 / 公开到项目交流）"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))

        # 权限检查：与编辑任务保持一致
        has_edit_permission = False
        if current_user.is_admin() or task.user_id == current_user.id:
            has_edit_permission = True
        elif task.organization_id:
            is_org_member = db.query(OrganizationMember).filter_by(
                organization_id=task.organization_id,
                user_id=current_user.id
            ).first() is not None
            if is_org_member:
                has_edit_permission = True
        else:
            is_shared = db.query(TaskShare).filter_by(
                task_id=task.id,
                user_id=current_user.id
            ).first() is not None
            if is_shared:
                has_edit_permission = True

        if not has_edit_permission:
            flash('无权修改该任务的公开范围', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task.id))

        visibility = request.form.get('visibility', '').strip()
        message = None

        if visibility == 'public':
            # 只有管理员或通过教师认证的用户可以公开到项目交流；公开后需管理员审核通过才会在项目交流展示
            if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                task.sharing_type = 'public'
                task.public_approved = 0  # 待管理员审核
                message = '已申请公开到项目交流，审核通过后将展示在项目交流页。'
            else:
                flash('只有通过教师认证的用户才能公开项目到共享区', 'warning')
                # 回退为私有/组织可见
                task.sharing_type = 'organization' if task.organization_id else 'private'
        else:
            # 设置为仅自己/组织内部可见
            task.sharing_type = 'organization' if task.organization_id else 'private'
            task.public_approved = 0
            message = '项目已设置为仅自己或组织内部可见。'

        db.commit()
        if message:
            flash(message, 'success')
        return redirect(url_for('quickform.task_detail', task_id=task.id))
    finally:
        db.close()

@quickform_bp.route('/delete_task/<int:task_id>', methods=['POST'])
@login_required
def delete_task(task_id):
    """删除任务，同时删除所有相关的提交数据"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        if task.user_id != current_user.id:
            flash('无权删除此任务', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        # 显式删除所有相关的提交数据
        submissions = db.query(Submission).filter_by(task_id=task.id).all()
        submission_count = len(submissions)
        
        for submission in submissions:
            db.delete(submission)
        
        # 删除任务文件（如果存在）
        if task.file_path and os.path.exists(task.file_path):
            try:
                os.remove(task.file_path)
                logger.info(f"已删除任务文件: {task.file_path}")
            except Exception as e:
                logger.warning(f"删除任务文件失败: {task.file_path}, 错误: {str(e)}")
        
        # 删除任务
        db.delete(task)
        db.commit()
        
        if submission_count > 0:
            flash(f'任务已删除，同时删除了 {submission_count} 条提交数据', 'success')
            logger.info(f"用户 {current_user.id} 删除了任务 {task_id}，同时删除了 {submission_count} 条提交数据")
        else:
            flash('任务已删除', 'success')
            logger.info(f"用户 {current_user.id} 删除了任务 {task_id}")
        
        return redirect(url_for('quickform.dashboard'))
    except Exception as e:
        db.rollback()
        logger.exception("删除任务失败: %s", e)
        flash('删除任务失败，请稍后重试或联系管理员。', 'danger')
        return redirect(url_for('quickform.dashboard'))
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/toggle_status', methods=['POST'])
@login_required
def task_toggle_status(task_id):
    """仅任务所有者可调用：切换任务状态 正常/停用。停用后接口不再接受与返回数据。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            return jsonify({'success': False, 'message': '任务不存在'}), 404
        if task.user_id != current_user.id:
            return jsonify({'success': False, 'message': '仅任务所有者可调整状态'}), 403
        task.is_active = not getattr(task, 'is_active', True)
        db.commit()
        return jsonify({'success': True, 'is_active': task.is_active})
    except Exception as e:
        db.rollback()
        logger.exception("切换任务状态失败: %s", e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/ai_test')
@login_required
def ai_test_page():
    """AI模型测试页面"""
    return render_template('ai_test.html')

@quickform_bp.route('/api/email/send_code', methods=['POST'])
def api_send_email_code():
    """发送邮箱验证码"""
    try:
        data = request.get_json() or {}
        email = (data.get('email') or '').strip()

        if not email:
            return jsonify({'success': False, 'message': '请提供邮箱地址'}), 400

        # 简单邮箱格式校验
        import re
        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            return jsonify({'success': False, 'message': '邮箱格式不正确'}), 400

        # 生成6位验证码
        import random
        code = f"{random.randint(0, 999999):06d}"

        # 保存验证码
        set_email_code(email, code, ttl_seconds=600)

        # 发送邮件
        send_email_code(email, code)

        return jsonify({'success': True, 'message': '验证码已发送到邮箱，有效期10分钟'})
    except RuntimeError as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    except Exception as e:
        logger.exception("发送邮箱验证码异常: %s", e)
        return jsonify({'success': False, 'message': '验证码发送失败，请稍后再试。若持续失败请联系管理员。'}), 500

SUBMIT_RATE_LIMIT_WINDOW = 30   # seconds（教室/公开课场景下适当放宽窗口）
SUBMIT_RATE_LIMIT_THRESHOLD = 200  # 窗口内同一设备提交超过此次数则限流（设备指纹+任务维度）
SUBMIT_BLACKLIST_DURATION = 300  # seconds

rate_limit_cache = {}
all_rate_limit_cache = {}
rate_limit_lock = threading.Lock()
# /all 接口最小访问间隔（秒）：同一设备+同一任务在该间隔内重复读取会被短暂限流
ALL_RATE_LIMIT_MIN_INTERVAL = float(os.getenv('ALL_RATE_LIMIT_MIN_INTERVAL', '0.35') or '0.35')


def _build_client_fingerprint(req) -> str:
    """构建设备级指纹：优先显式设备ID，否则使用稳定请求头组合哈希。"""
    explicit_device_id = (
        (req.headers.get('X-QuickForm-Device-ID') or '').strip()
        or (req.headers.get('X-Device-ID') or '').strip()
        or (req.cookies.get('qf_device_id') or '').strip()
    )
    if explicit_device_id:
        safe = explicit_device_id[:128]
        return 'dev-' + hashlib.sha256(f'explicit:{safe}'.encode('utf-8')).hexdigest()[:24]

    ua = (req.headers.get('User-Agent') or '').strip()
    lang = (req.headers.get('Accept-Language') or '').strip()
    ch_ua = (req.headers.get('Sec-CH-UA') or '').strip()
    ch_platform = (req.headers.get('Sec-CH-UA-Platform') or '').strip()
    ch_mobile = (req.headers.get('Sec-CH-UA-Mobile') or '').strip()
    ip = get_request_client_ip(req)
    base = '|'.join([ua, lang, ch_ua, ch_platform, ch_mobile, ip])
    return 'fp-' + hashlib.sha256(base.encode('utf-8')).hexdigest()[:24]


def _submit_subject_key(req, task_id: str):
    fp = _build_client_fingerprint(req)
    ip = get_request_client_ip(req)
    return f'submit:{task_id}:{fp}', fp, ip


def _all_subject_key(req, task_id: str):
    fp = _build_client_fingerprint(req)
    ip = get_request_client_ip(req)
    return f'all:{task_id}:{fp}', fp, ip


def _generate_submission_manage_code() -> str:
    """生成任务级删改认证码。"""
    # 生成更长随机串，降低被猜测/爆破风险（约 32+ chars）
    return secrets.token_urlsafe(24)


def _extract_submission_manage_code() -> str:
    """从查询参数、表单或请求头中提取删改认证码。"""
    return (
        (request.args.get('edit_code') or '').strip()
        or (request.form.get('edit_code') or '').strip()
        or (request.headers.get('X-QuickForm-Edit-Code') or '').strip()
    )


def _can_manage_task_submissions(db, task, auth_code: str = ''):
    """是否允许删改任务提交：教师原有编辑权限，或提供正确的任务删改认证码。"""
    if not task:
        return False
    if auth_code and getattr(task, 'submission_manage_code', None):
        return secrets.compare_digest((task.submission_manage_code or '').strip(), auth_code.strip())
    if not getattr(current_user, 'is_authenticated', False):
        return False
    share_rec = db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first()
    org_mem = db.query(OrganizationMember).filter_by(
        organization_id=task.organization_id, user_id=current_user.id
    ).first() if task.organization_id else None
    return (
        current_user.is_admin() or task.user_id == current_user.id or
        (org_mem and _org_members_can_edit_tasks(db, task.organization_id)) or
        (share_rec and share_rec.can_edit)
    )


# 删改接口限频（防爆破/防刷）：按 任务+设备 指纹
MANAGE_RATE_LIMIT_WINDOW = float(os.getenv('MANAGE_RATE_LIMIT_WINDOW', '10') or '10')  # seconds
MANAGE_RATE_LIMIT_THRESHOLD = int(os.getenv('MANAGE_RATE_LIMIT_THRESHOLD', '40') or '40')  # window max
_manage_rate_limit_cache = {}
_manage_rate_limit_lock = threading.Lock()


def _manage_rate_limit_check(task_id: int) -> bool:
    """返回 True 表示允许；False 表示应限流。"""
    try:
        fp = _build_client_fingerprint(request)
    except Exception:
        fp = 'unknown'
    key = f'manage:{task_id}:{fp}'
    now_ts = datetime.utcnow().timestamp()
    with _manage_rate_limit_lock:
        dq = _manage_rate_limit_cache.setdefault(key, deque())
        while dq and now_ts - dq[0] > MANAGE_RATE_LIMIT_WINDOW:
            dq.popleft()
        dq.append(now_ts)
        return len(dq) <= MANAGE_RATE_LIMIT_THRESHOLD

# 单任务 /all 默认限额兜底（库表 site_quota_default 无记录或异常时使用；正常以管理后台「全局限额默认值」为准）
_TASK_ALL_READ_FALLBACK = 2000
_TASK_ALL_BYTES_FALLBACK = 100 * 1024 * 1024

# 单任务“提交写入”默认限额兜底（防刷库/防存爆）
_TASK_SUBMIT_COUNT_FALLBACK = 100000
_TASK_SUBMIT_BYTES_FALLBACK = 500 * 1024 * 1024  # 500MB


def _get_site_all_quota_defaults(db):
    """读取全站默认：每任务 /all 基础次数与字节上限。有效限额 = 此处返回值 + task.quota_extra_*。"""
    try:
        row = db.get(SiteQuotaDefault, 1)
        if row is not None:
            r = int(row.default_all_read_limit or 0)
            b = int(row.default_all_bytes_limit or 0)
            if r >= 1 and b >= 1024 * 1024:
                return r, b
    except Exception as ex:
        logger.warning("读取全站 /all 默认限额失败: %s", ex)
    return _TASK_ALL_READ_FALLBACK, _TASK_ALL_BYTES_FALLBACK


def _get_site_submit_quota_defaults(db):
    """读取全站默认：每任务提交写入的基础条数与字节上限。有效限额 = 此处返回值 + task.quota_extra_submit_*。"""
    try:
        row = db.get(SiteQuotaDefault, 1)
        if row is not None:
            c = int(getattr(row, 'default_submit_count_limit', 0) or 0)
            b = int(getattr(row, 'default_submit_bytes_limit', 0) or 0)
            # c==0 表示不限次数；b 仍要求至少 1MB
            if c >= 0 and b >= 1024 * 1024:
                return c, b
    except Exception as ex:
        logger.warning("读取全站提交默认限额失败: %s", ex)
    return _TASK_SUBMIT_COUNT_FALLBACK, _TASK_SUBMIT_BYTES_FALLBACK


# ---------- 接口 GET 次数统计（管理员流量预估）----------
_api_get_counts = {}  # 接口类别 -> GET 次数，如 api_task_get / api_task_all / api_tasks
_api_counts_lock = threading.Lock()


def _record_api_get(category):
    """记录一次 API GET 请求，用于管理员流量预估"""
    with _api_counts_lock:
        _api_get_counts[category] = _api_get_counts.get(category, 0) + 1


# ---------- CLI 接口（供命令行 / 扣子 / OpenClaw 等自动化调用，原 MCP 已统一改名为 CLI）----------

def _cli_doc_view():
    """展示 CLI 接口教程，从 docs/CLI接口说明.md 读取并渲染"""
    import markdown
    doc_path = os.path.join(QUICKFORM_DIR, '..', 'docs', 'CLI接口说明.md')
    doc_path = os.path.normpath(os.path.abspath(doc_path))
    if not os.path.isfile(doc_path):
        return (
            '<!DOCTYPE html><html><head><meta charset="utf-8"><title>CLI 说明</title></head><body>'
            '<p>未找到教程文档：QuickForm/docs/CLI接口说明.md</p></body></html>',
            200,
            [('Content-Type', 'text/html; charset=utf-8')]
        )
    try:
        with open(doc_path, 'r', encoding='utf-8') as f:
            md_text = f.read()
        body_html = markdown.markdown(
            md_text,
            extensions=['extra', 'nl2br', 'tables', 'fenced_code'],
            extension_configs={'tables': {}},
        )
        html_page = (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>CLI 接口说明 - QuickForm</title>'
            '<link href="' + url_for('static', filename='css/bootstrap.min.css') + '" rel="stylesheet">'
            '<style>.cli-body{max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.7}.cli-body h1{font-size:1.5rem;border-bottom:1px solid #dee2e6;padding-bottom:.25rem}.cli-body h2{font-size:1.25rem;margin-top:1.25rem}.cli-body table{border-collapse:collapse;width:100%;margin:1rem 0}.cli-body th,.cli-body td{border:1px solid #dee2e6;padding:.5rem .75rem;text-align:left}.cli-body th{background:#f8f9fa}.cli-body pre{background:#f6f8fa;padding:1rem;border-radius:6px;overflow-x:auto}.cli-body code{background:#f0f0f0;padding:.2em .4em;border-radius:4px}</style></head><body>'
            '<div class="container py-3"><a href="' + url_for('quickform.index') + '" class="btn btn-outline-primary btn-sm">← 返回首页</a></div>'
            '<div class="cli-body">' + body_html + '</div></body></html>'
        )
        return make_response(html_page, 200, [('Content-Type', 'text/html; charset=utf-8')])
    except Exception as e:
        logger.exception('CLI doc render failed')
        return (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>说明</title></head>'
            '<body><p>文档暂时无法显示，请稍后重试或联系管理员。</p></body></html>',
            500,
            [('Content-Type', 'text/html; charset=utf-8')],
        )


@quickform_bp.route('/cli', methods=['GET'])
def cli_doc():
    """访问 /cli 时展示 CLI 接口教程"""
    return _cli_doc_view()


@quickform_bp.route('/mcp', methods=['GET'])
def mcp_doc():
    """兼容旧链接：重定向到 /cli"""
    return redirect(url_for('quickform.cli_doc'))


def _mcp_parse_body():
    """解析请求体：支持 application/json 或 application/x-www-form-urlencoded"""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def _mcp_authenticate(username, password):
    """用户名+密码认证，返回 User 或 None。不写 session，仅做校验。"""
    if not username or not password:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(
            (User.username == username) |
            (User.email == username) |
            (User.phone == username)
        ).first()
        if user and bcrypt.check_password_hash(user.password, password):
            return user
        return None
    finally:
        db.close()


def _cert_apply_approve(db, cert_request, reviewer_user_id, note=''):
    """教师认证「通过」：与网页管理员审核逻辑一致（单条，未 commit）。"""
    user = cert_request.user
    if not user:
        return False, 'no_applicant'
    if cert_request.status == 1:
        return True, 'already_approved'
    note = (note or '').strip()
    cert_request.status = 1
    cert_request.reviewed_at = datetime.now()
    cert_request.reviewed_by = reviewer_user_id
    cert_request.review_note = note
    user.is_certified = True
    user.certified_at = datetime.now()
    user.certification_note = note
    if user.task_limit != -1:
        user.task_limit = -1
    pending_tasks = db.query(Task).filter(Task.user_id == user.id, Task.html_approved != 1).all()
    for task in pending_tasks:
        task.html_approved = 1
        task.html_approved_by = reviewer_user_id
        task.html_approved_at = datetime.now()
        task.html_review_note = None
    return True, 'ok'


def _cert_apply_reject(db, cert_request, reviewer_user_id, note=''):
    """教师认证「拒绝」：与网页管理员审核逻辑一致（单条，未 commit）。"""
    if cert_request.status == -1:
        return True, 'already_rejected'
    note = (note or '').strip()
    cert_request.status = -1
    cert_request.reviewed_at = datetime.now()
    cert_request.reviewed_by = reviewer_user_id
    cert_request.review_note = note
    return True, 'ok'


def _cli_login_throttle_reject_if_blocked(request, login_name: Optional[str]):
    """CLI 撞库/暴力尝试防护：与网页登录共用 login_throttle。在验证密码前调用。
    命中限流时返回 (response, 429)，否则 None。"""
    name = (login_name or '').strip() or None
    blocked, retry_after = login_blocked(request, name)
    if not blocked:
        return None
    minutes = max(1, (retry_after + 59) // 60)
    return (
        jsonify({
            'success': False,
            'error': 'rate_limit',
            'message': f'尝试过于频繁，请约 {minutes} 分钟后再试',
            'retry_after': retry_after,
        }),
        429,
    )


def _cli_record_credential_failure(request, login_name: Optional[str]):
    """CLI 用户名/密码校验失败（含管理员凭据错误、普通用户密码错误）。"""
    record_login_failure(request, (login_name or '').strip() or None)


def _cli_clear_credential_throttle(request, login_name: Optional[str]):
    """CLI 凭据校验成功后清理限流计数（与网页登录成功一致）。"""
    clear_login_throttle(request, (login_name or '').strip() or None)


def _cli_require_admin(data):
    """CLI 管理员校验：返回 (admin_user, None) 或 (None, (jsonify_err, status))。"""
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return None, (jsonify({'success': False, 'message': '缺少 username 或 password'}), 400)
    rej = _cli_login_throttle_reject_if_blocked(request, username)
    if rej:
        return None, rej
    user = _mcp_authenticate(username, password)
    if not user or not user.is_admin():
        _cli_record_credential_failure(request, username)
        return None, (jsonify({'success': False, 'message': '权限不足或管理员凭据错误'}), 403)
    _cli_clear_credential_throttle(request, username)
    return user, None


@quickform_bp.route('/cli/add', methods=['POST'])
@quickform_bp.route('/mcp/add', methods=['POST'])
def cli_add_task():
    """
    增加数据任务。
    参数：username, password, task_name（任务名称）, task_intro（任务介绍，可选）。
    返回：{ "success": true, "apiid": "<task_id>" } 或 { "success": false, "message": "..." }。
    """
    data = _mcp_parse_body()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    task_name = (data.get('task_name') or data.get('title') or '').strip()
    task_intro = (data.get('task_intro') or data.get('description') or '').strip()

    if not username or not password:
        return jsonify({'success': False, 'message': '缺少 username 或 password'}), 400
    if not task_name:
        return jsonify({'success': False, 'message': '缺少 task_name（任务名称）'}), 400

    rej = _cli_login_throttle_reject_if_blocked(request, username)
    if rej:
        return rej
    user = _mcp_authenticate(username, password)
    if not user:
        _cli_record_credential_failure(request, username)
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401
    _cli_clear_credential_throttle(request, username)

    db = SessionLocal()
    try:
        task_count = db.query(Task).filter_by(user_id=user.id).count()
        if not user.can_create_task(SessionLocal, Task):
            return jsonify({
                'success': False,
                'message': f'已达任务数量上限（当前 {task_count} 个），无法创建新任务'
            }), 403
        block = _email_requirement_block_for_next_task(db, user, task_count)
        if block == 'bind_email':
            return jsonify({
                'success': False,
                'code': 'email_not_bound',
                'message': '创建第二个及后续任务前请先在网站「个人资料」中绑定真实邮箱。',
            }), 403
        if block == 'verify_email':
            return jsonify({
                'success': False,
                'code': 'email_not_verified',
                'message': '创建第二个及后续任务前请先完成邮箱验证（网站内「验证邮箱」页面）。',
            }), 403
        task = Task(title=task_name, description=task_intro or None, user_id=user.id)
        db.add(task)
        db.commit()
        db.refresh(task)
        return jsonify({'success': True, 'apiid': task.task_id}), 200
    except Exception as e:
        db.rollback()
        logger.exception('CLI add task failed')
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/cli/reset_user_password', methods=['POST'])
@quickform_bp.route('/mcp/reset_user_password', methods=['POST'])
def cli_reset_user_password():
    """
    管理员重置指定用户密码（CLI，无 Cookie）。
    参数：username, password（管理员账号）, new_password（至少 6 字符）,
         以及 target_username 或 target_user_id（二选一）。
    """
    data = _mcp_parse_body()
    admin_name = (data.get('username') or '').strip()
    admin_pass = data.get('password') or ''
    new_password = (data.get('new_password') or '').strip()
    target_username = (data.get('target_username') or data.get('target') or '').strip()
    target_user_id = data.get('target_user_id')

    if not admin_name or not admin_pass:
        return jsonify({'success': False, 'message': '缺少 username 或 password'}), 400
    if not new_password or len(new_password) < 6:
        return jsonify({'success': False, 'message': 'new_password 长度至少为 6 个字符'}), 400
    if not target_username and target_user_id is None:
        return jsonify({'success': False, 'message': '请提供 target_username 或 target_user_id'}), 400

    rej = _cli_login_throttle_reject_if_blocked(request, admin_name)
    if rej:
        return rej
    admin_user = _mcp_authenticate(admin_name, admin_pass)
    if not admin_user or not admin_user.is_admin():
        _cli_record_credential_failure(request, admin_name)
        return jsonify({'success': False, 'message': '权限不足或管理员凭据错误'}), 403
    _cli_clear_credential_throttle(request, admin_name)

    db = SessionLocal()
    try:
        if target_user_id is not None:
            try:
                tid = int(target_user_id)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'message': 'target_user_id 无效'}), 400
            target = db.get(User, tid)
        else:
            target = db.query(User).filter(User.username == target_username).first()
        if not target:
            return jsonify({'success': False, 'message': '目标用户不存在'}), 404
        if target.id == admin_user.id:
            return jsonify({'success': False, 'message': '不能通过此接口重置自己的密码'}), 400

        target.password = bcrypt.generate_password_hash(new_password).decode('utf-8')
        db.commit()
        return jsonify({
            'success': True,
            'message': '密码已重置',
            'username': target.username,
            'user_id': target.id,
        }), 200
    except Exception:
        db.rollback()
        logger.exception('CLI reset_user_password failed')
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/cli/set_user_email', methods=['POST'])
@quickform_bp.route('/mcp/set_user_email', methods=['POST'])
def cli_set_user_email():
    """
    管理员修改指定用户邮箱（CLI，无 Cookie）。修改后该用户 email_verified 会置为未验证。
    参数：username, password（管理员）, new_email, target_username 或 target_user_id（二选一）。
    """
    data = _mcp_parse_body()
    admin_name = (data.get('username') or '').strip()
    admin_pass = data.get('password') or ''
    new_email = (data.get('new_email') or '').strip()
    target_username = (data.get('target_username') or data.get('target') or '').strip()
    target_user_id = data.get('target_user_id')

    if not admin_name or not admin_pass:
        return jsonify({'success': False, 'message': '缺少 username 或 password'}), 400
    if not new_email:
        return jsonify({'success': False, 'message': '缺少 new_email'}), 400
    if not target_username and target_user_id is None:
        return jsonify({'success': False, 'message': '请提供 target_username 或 target_user_id'}), 400

    rej = _cli_login_throttle_reject_if_blocked(request, admin_name)
    if rej:
        return rej
    admin_user = _mcp_authenticate(admin_name, admin_pass)
    if not admin_user or not admin_user.is_admin():
        _cli_record_credential_failure(request, admin_name)
        return jsonify({'success': False, 'message': '权限不足或管理员凭据错误'}), 403
    _cli_clear_credential_throttle(request, admin_name)

    db = SessionLocal()
    try:
        actor = db.get(User, admin_user.id)
        if not actor:
            return jsonify({'success': False, 'message': '管理员账号异常'}), 500
        if target_user_id is not None:
            try:
                tid = int(target_user_id)
            except (TypeError, ValueError):
                return jsonify({'success': False, 'message': 'target_user_id 无效'}), 400
            target = db.get(User, tid)
        else:
            target = db.query(User).filter(User.username == target_username).first()
        if not target:
            return jsonify({'success': False, 'message': '目标用户不存在'}), 404

        ok, code = _admin_apply_user_email_change(db, target, actor, new_email)
        if not ok:
            return jsonify({'success': False, 'message': code}), 400
        if code == 'unchanged':
            return jsonify({
                'success': True,
                'message': '邮箱未变化',
                'username': target.username,
                'user_id': target.id,
                'email': target.email,
                'email_verified': getattr(target, 'email_verified', False),
            }), 200
        db.commit()
        return jsonify({
            'success': True,
            'message': '邮箱已更新，该用户需重新验证邮箱',
            'username': target.username,
            'user_id': target.id,
            'email': target.email,
            'email_verified': False,
        }), 200
    except Exception:
        db.rollback()
        logger.exception('CLI set_user_email failed')
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/cli/cert_pending', methods=['POST'])
@quickform_bp.route('/mcp/cert_pending', methods=['POST'])
def cli_cert_pending():
    """
    管理员：列出待审核的教师认证申请（材料元数据）。
    参数：username, password, limit（可选，默认 50，最大 200）。
    下载原文件请用 POST /cli/cert_material（管理员账号 + request_id）。
    """
    data = _mcp_parse_body()
    _admin, err = _cli_require_admin(data)
    if err:
        return err
    limit = data.get('limit', 50)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    db = SessionLocal()
    try:
        rows = (
            db.query(CertificationRequest)
            .filter(CertificationRequest.status == 0)
            .order_by(CertificationRequest.created_at.asc())
            .limit(limit)
            .all()
        )
        base = (request.url_root or '').rstrip('/')
        material_path = url_for('quickform.cli_cert_material')
        material_url = base + material_path
        items = []
        for r in rows:
            u = r.user
            fn = (r.file_name or '')
            fp = (r.file_path or '')
            ext = ''
            if fn and '.' in fn:
                ext = fn.rsplit('.', 1)[-1].lower()
            elif fp and '.' in fp:
                ext = os.path.basename(fp).rsplit('.', 1)[-1].lower()
            items.append({
                'request_id': r.id,
                'user_id': r.user_id,
                'username': u.username if u else None,
                'school': (u.school if u else None) or '',
                'phone': (u.phone if u else None) or '',
                'email': (u.email if u else None) or '',
                'file_name': r.file_name or '',
                'file_ext': ext,
                'has_file': bool(r.file_path and os.path.exists(r.file_path)),
                'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else '',
            })
        return jsonify({
            'success': True,
            'count': len(items),
            'items': items,
            'material_hint': '使用相同管理员凭据 POST /cli/cert_material，JSON 字段 request_id 下载对应材料文件。',
            'material_url': material_url,
        }), 200
    finally:
        db.close()


@quickform_bp.route('/cli/cert_material', methods=['POST'])
@quickform_bp.route('/mcp/cert_material', methods=['POST'])
def cli_cert_material():
    """管理员：按 request_id 下载教师认证上传的原始文件（附件形式，便于 curl -o）。"""
    data = _mcp_parse_body()
    _admin, err = _cli_require_admin(data)
    if err:
        return err
    rid = data.get('request_id')
    try:
        rid_int = int(rid)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': '缺少或无效的 request_id'}), 400

    db = SessionLocal()
    try:
        cert_request = db.get(CertificationRequest, rid_int)
        if not cert_request or not cert_request.file_path or not os.path.exists(cert_request.file_path):
            return jsonify({'success': False, 'message': '认证材料不存在或文件已删除'}), 404
        filename = os.path.basename(cert_request.file_path)
        try:
            return send_file(
                cert_request.file_path,
                download_name=filename or 'certification.bin',
                as_attachment=True,
            )
        except TypeError:
            return send_file(
                cert_request.file_path,
                attachment_filename=filename or 'certification.bin',
                as_attachment=True,
            )
    finally:
        db.close()


@quickform_bp.route('/cli/cert_decide', methods=['POST'])
@quickform_bp.route('/mcp/cert_decide', methods=['POST'])
def cli_cert_decide():
    """
    管理员：通过或拒绝一条待审核的教师认证申请。
    参数：username, password, request_id, action（approve 或 reject）, note（可选，备注）。
    """
    data = _mcp_parse_body()
    admin, err = _cli_require_admin(data)
    if err:
        return err
    rid = data.get('request_id')
    action = (data.get('action') or '').strip().lower()
    note = (data.get('note') or '').strip()
    try:
        rid_int = int(rid)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': '缺少或无效的 request_id'}), 400
    if action not in ('approve', 'reject'):
        return jsonify({'success': False, 'message': 'action 须为 approve 或 reject'}), 400

    db = SessionLocal()
    try:
        cert_request = db.get(CertificationRequest, rid_int)
        if not cert_request:
            return jsonify({'success': False, 'message': '认证申请不存在'}), 404
        if cert_request.status != 0:
            return jsonify({
                'success': False,
                'message': '该申请已处理，非待审核状态',
                'status': cert_request.status,
            }), 409

        applicant = cert_request.user
        if action == 'approve':
            ok, _code = _cert_apply_approve(db, cert_request, admin.id, note)
            if not ok:
                return jsonify({'success': False, 'message': '无法通过（申请人数据异常）'}), 400
            db.commit()
            return jsonify({
                'success': True,
                'message': '已通过该教师认证申请',
                'request_id': cert_request.id,
                'username': applicant.username if applicant else None,
                'user_id': applicant.id if applicant else None,
            }), 200

        _cert_apply_reject(db, cert_request, admin.id, note)
        db.commit()
        return jsonify({
            'success': True,
            'message': '已拒绝该认证申请',
            'request_id': cert_request.id,
            'username': applicant.username if applicant else None,
        }), 200
    except Exception:
        db.rollback()
        logger.exception('CLI cert_decide failed')
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/cli/list', methods=['POST'])
@quickform_bp.route('/mcp/list', methods=['POST'])
def cli_list_tasks():
    """
    查看数据任务列表。
    参数：username, password。
    返回：{ "success": true, "tasks": [ { "apiid": "<task_id>", "name": "<title>" }, ... ] } 或错误。
    """
    data = _mcp_parse_body()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return jsonify({'success': False, 'message': '缺少 username 或 password'}), 400

    rej = _cli_login_throttle_reject_if_blocked(request, username)
    if rej:
        return rej
    user = _mcp_authenticate(username, password)
    if not user:
        _cli_record_credential_failure(request, username)
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401
    _cli_clear_credential_throttle(request, username)

    db = SessionLocal()
    try:
        tasks = db.query(Task).filter_by(user_id=user.id).order_by(Task.created_at.desc()).all()
        out = [{'apiid': t.task_id, 'name': t.title or ''} for t in tasks]
        return jsonify({'success': True, 'tasks': out}), 200
    finally:
        db.close()


@quickform_bp.route('/cli/show', methods=['POST'])
@quickform_bp.route('/mcp/show', methods=['POST'])
def cli_show_task():
    """
    查看单个任务详情（CLI，无 Cookie）。
    参数：username, password, apiid（或 task_id / taskid；也支持 id=数据库自增ID）。
    返回：任务名称、简介、url、教程、附件地址等。
    """
    data = _mcp_parse_body()
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    apiid = (data.get('apiid') or data.get('task_id') or data.get('taskid') or '').strip()
    db_id = data.get('id')

    if not username or not password:
        return jsonify({'success': False, 'message': '缺少 username 或 password'}), 400
    if not apiid and db_id is None:
        return jsonify({'success': False, 'message': '缺少 apiid（任务 APIID）或 id（任务数据库ID）'}), 400

    rej = _cli_login_throttle_reject_if_blocked(request, username)
    if rej:
        return rej
    user = _mcp_authenticate(username, password)
    if not user:
        _cli_record_credential_failure(request, username)
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401
    _cli_clear_credential_throttle(request, username)

    def _abs(u: str) -> str:
        u = (u or '').strip()
        if not u:
            return ''
        if u.startswith('http://') or u.startswith('https://'):
            return u
        base = _public_site_base_url().rstrip('/')
        return base + u

    db = SessionLocal()
    try:
        task = None
        if apiid:
            task = db.query(Task).filter_by(task_id=apiid, user_id=user.id).first()
        if not task and db_id is not None:
            try:
                tid_int = int(db_id)
            except (TypeError, ValueError):
                tid_int = None
            if tid_int:
                t2 = db.get(Task, tid_int)
                if t2 and t2.user_id == user.id:
                    task = t2
        if not task:
            return jsonify({'success': False, 'message': '任务不存在或无权限'}), 404

        api_base = _public_site_base_url().rstrip('/')
        api_url = f"{api_base}/api/{task.task_id}"
        all_url = f"{api_base}/api/{task.task_id}/all"
        detail_url = f"{api_base}{url_for('quickform.task_detail', task_id=task.id)}"

        attachments = []
        # 任务 HTML 附件（兼容单文件与多文件）
        for f in _task_html_file_links(task):
            if isinstance(f, dict) and f.get('url'):
                attachments.append({
                    'name': (f.get('name') or '').strip() or 'form.html',
                    'url': _abs(f.get('url')),
                })
        # 数据大屏附件（如果已生成）
        dash_saved = getattr(task, 'dashboard_saved_name', None)
        if dash_saved:
            attachments.append({
                'name': '数据大屏.html',
                'url': f"{api_base}/static/uploads/{dash_saved}",
            })

        return jsonify({
            'success': True,
            'apiid': task.task_id,
            'name': task.title or '',
            'intro': task.description or '',
            'url': api_url,
            'all_url': all_url,
            'task_detail_url': detail_url,
            'tutorial': (getattr(task, 'tutorial_link', None) or '').strip(),
            'share_url': (getattr(task, 'share_url', None) or '').strip(),
            'attachments': attachments,
        }), 200
    finally:
        db.close()


@quickform_bp.route('/cli/upload', methods=['POST'])
@quickform_bp.route('/mcp/upload', methods=['POST'])
def cli_upload_html():
    """
    上传 HTML 文件，返回上传结果与文件公网地址。
    请求：multipart/form-data，字段 username, password, file（.html/.htm，单文件最大 4MB）。
    返回：{ "success": true, "url": "https://.../static/uploads/xxx.html", "filename": "xxx.html" } 或错误。
    """
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    client_key = _upload_auth_client_key(username)
    if _upload_auth_is_blocked(client_key):
        return jsonify({'success': False, 'message': '认证失败次数过多，请稍后再试'}), 429
    if not username or not password:
        _upload_auth_record_failure(client_key)
        return jsonify({'success': False, 'message': '缺少 username 或 password'}), 400

    rej = _cli_login_throttle_reject_if_blocked(request, username)
    if rej:
        return rej
    user = _mcp_authenticate(username, password)
    if not user:
        _cli_record_credential_failure(request, username)
        _upload_auth_record_failure(client_key)
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401
    _cli_clear_credential_throttle(request, username)
    _upload_auth_clear_failures(client_key)

    file = request.files.get('file')
    if not file or not (file.filename or '').strip():
        return jsonify({'success': False, 'message': '请选择要上传的 HTML 文件（字段名 file）'}), 400

    if not allowed_file(file.filename, ALLOWED_EXTENSIONS):
        return jsonify({'success': False, 'message': '仅支持 .html 或 .htm 文件'}), 400

    try:
        upload_dir = _static_uploads_dir()
        unique_filename, filepath = save_uploaded_file(file, upload_dir, ALLOWED_EXTENSIONS)
        if not unique_filename or not filepath:
            return jsonify({'success': False, 'message': '文件保存失败或格式不支持'}), 400
        if filepath.lower().endswith(('.html', '.htm')) and os.path.getsize(filepath) > MAX_HTML_FILE_SIZE:
            try:
                os.remove(filepath)
            except OSError:
                pass
            return jsonify({'success': False, 'message': '单个 HTML 文件不得超过 4MB'}), 400
        public_url = url_for('static', filename='uploads/' + unique_filename, _external=True)
        return jsonify({
            'success': True,
            'url': public_url,
            'filename': unique_filename,
        }), 200
    except Exception as e:
        logger.exception('CLI upload failed')
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500


@quickform_bp.route('/api/<string:task_id>', methods=['GET', 'POST', 'OPTIONS'])
def submit_form(task_id):
    """表单提交API - 支持GET查询和POST提交"""
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Content-Type'] = 'text/plain; charset=utf-8'
        return response
        
    db = SessionLocal()
    try:
        task = db.query(Task).filter_by(task_id=task_id).first()
        if not task:
            response = jsonify({
                'error': 'task_not_found',
                'task_id': task_id,
                'message': f'No task found for task_id "{task_id}".',
            })
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            logger.warning(f"请求失败: 任务不存在 - task_id: {task_id}")
            return response, 404
        if not getattr(task, 'is_active', True):
            response = jsonify({
                'error': 'task_disabled',
                'task_id': task_id,
                'message': 'This task is disabled. Submissions and data reads are not accepted.',
            })
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response, 403
        
        # GET方法：返回任务数据统计（只返回最新的3条）
        if request.method == 'GET':
            cache_key = _build_read_cache_key('api_task_get', task_id)
            cached_payload = _cache_read_get(cache_key)
            if cached_payload is not None:
                response = jsonify(cached_payload)
                response.headers['Access-Control-Allow-Origin'] = '*'
                response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
                response.headers['Cache-Control'] = 'no-store'
                return response, 200

            # 只获取最新的3条数据
            submissions = db.query(Submission).filter_by(task_id=task.id).order_by(Submission.submitted_at.desc()).limit(3).all()
            total_count = db.query(Submission).filter_by(task_id=task.id).count()
            data_list = []
            for sub in submissions:
                try:
                    # 尝试解析JSON数据
                    data = json.loads(sub.data)
                    # 如果解析后是字符串，可能是双重编码，再解析一次
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except:
                            pass
                    data['submitted_at'] = sub.submitted_at.strftime('%Y-%m-%d %H:%M:%S')
                    data_list.append(data)
                except (json.JSONDecodeError, TypeError):
                    # 如果解析失败，返回原始数据作为raw_data
                    data_list.append({
                        'submitted_at': sub.submitted_at.strftime('%Y-%m-%d %H:%M:%S'),
                        'raw_data': sub.data
                    })
            
            # 构建/all路由的完整URL
            base_url = request.url.rstrip('/')
            all_url = f"{base_url}/all"
            _record_api_get('api_task_get')
            response = jsonify({
                'note': f'This endpoint returns the 3 most recent submissions. Full list: {all_url}',
                'task_id': task.task_id,
                'task_title': task.title,
                'total_submissions': total_count,
                'submissions': data_list
            })
            _cache_read_set(cache_key, {
                'note': f'This endpoint returns the 3 most recent submissions. Full list: {all_url}',
                'task_id': task.task_id,
                'task_title': task.title,
                'total_submissions': total_count,
                'submissions': data_list
            })
            try:
                db.execute(
                    text("UPDATE task SET api_task_get_count = COALESCE(api_task_get_count, 0) + 1 WHERE id = :id"),
                    {"id": task.id},
                )
                db.commit()
            except Exception as cnt_err:
                db.rollback()
                logger.warning("api_task_get_count 更新失败: %s", cnt_err)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            response.headers['Cache-Control'] = 'no-store'
            return response, 200
        
        # POST方法：提交数据
        subject_key, client_fp, client_ip = _submit_subject_key(request, task_id)
        now_ts = datetime.utcnow().timestamp()
        over_limit = False
        in_blacklist = False

        with rate_limit_lock:
            info = rate_limit_cache.setdefault(subject_key, {
                'events': deque(),
                'blacklist_until': 0,
            })
            if info['blacklist_until'] and now_ts < info['blacklist_until']:
                in_blacklist = True
            else:
                events: Deque = info['events']
                while events and now_ts - events[0] > SUBMIT_RATE_LIMIT_WINDOW:
                    events.popleft()
                events.append(now_ts)
                if len(events) > SUBMIT_RATE_LIMIT_THRESHOLD:
                    info['blacklist_until'] = now_ts + SUBMIT_BLACKLIST_DURATION
                    over_limit = True

        # 检查黑名单（按 设备+任务 精准封禁，避免同出口IP连坐）
        if in_blacklist:
            logger.warning(
                "设备 %s（IP %s）正在黑名单中，拒绝 task_id=%s 的提交",
                client_fp, client_ip, task_id
            )
            return _rate_limit_response(task_id, client_ip, client_fp, now_ts, db)
        
        # 获取提交的数据
        try:
            if request.is_json:
                form_data = request.get_json(silent=True)
                if form_data is None:
                    raw_body = request.get_data(cache=True, as_text=True) or ''
                    if not raw_body.strip():
                        response = jsonify({'error': 'invalid_body', 'message': MSG_JSON_BODY})
                        response.headers['Access-Control-Allow-Origin'] = '*'
                        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
                        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
                        return response, 400
                    try:
                        # 兼容部分网页把多行文本直接拼进 JSON，导致出现未转义控制字符的情况
                        form_data = json.loads(raw_body, strict=False)
                    except Exception as json_err:
                        logger.warning("解析JSON请求失败（task_id=%s）: %s", task_id, json_err)
                        response = jsonify({
                            'error': 'invalid_body',
                            'message': '提交失败：JSON 格式不正确。若包含多行文本，请确保换行已正确转义；不要直接提交未处理的图片/Base64大文本。'
                        })
                        response.headers['Access-Control-Allow-Origin'] = '*'
                        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
                        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
                        return response, 400
            else:
                form_data = request.form.to_dict()
        except Exception as e:
            logger.warning("解析请求数据失败(task_id=%s): %s", task_id, e)
            response = jsonify({'error': 'invalid_body', 'message': MSG_JSON_BODY})
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response, 400
        
        if over_limit:
            logger.warning(
                "设备 %s（IP %s）在 %ss 内提交过快，已加入黑名单 %ss",
                client_fp, client_ip, SUBMIT_RATE_LIMIT_WINDOW, SUBMIT_BLACKLIST_DURATION
            )
            return _rate_limit_response(task_id, client_ip, client_fp, now_ts, db)
        
        # 将数据转换为JSON字符串存储
        try:
            data_text = json.dumps(form_data, ensure_ascii=False)
            payload_bytes = len((data_text or '').encode('utf-8', errors='ignore'))

            # 单任务提交写入限额（防刷库/防存爆）：原子判断 + 计数累加
            base_c, base_b = _get_site_submit_quota_defaults(db)
            lim_c = int(base_c) + int(getattr(task, 'quota_extra_submit_count', 0) or 0)
            lim_b = int(base_b) + int(getattr(task, 'quota_extra_submit_bytes', 0) or 0)
            # lim_c <= 0 表示「不限提交次数」（只校验累计体积）
            if lim_c <= 0:
                upd = db.execute(
                    text("""
                        UPDATE task SET
                            submission_count_total = COALESCE(submission_count_total, 0) + 1,
                            submission_bytes_total = COALESCE(submission_bytes_total, 0) + :pb
                        WHERE id = :tid
                          AND COALESCE(submission_bytes_total, 0) + :pb <= :lim_b
                    """),
                    {"tid": task.id, "pb": payload_bytes, "lim_b": lim_b},
                )
            else:
                upd = db.execute(
                    text("""
                        UPDATE task SET
                            submission_count_total = COALESCE(submission_count_total, 0) + 1,
                            submission_bytes_total = COALESCE(submission_bytes_total, 0) + :pb
                        WHERE id = :tid
                          AND COALESCE(submission_count_total, 0) < :lim_c
                          AND COALESCE(submission_bytes_total, 0) + :pb <= :lim_b
                    """),
                    {"tid": task.id, "pb": payload_bytes, "lim_c": lim_c, "lim_b": lim_b},
                )
            if not upd.rowcount:
                db.rollback()
                lim_c_text = '不限' if lim_c <= 0 else f'{lim_c} 条'
                response = jsonify({
                    'error': 'quota_exceeded',
                    'message': f'提交失败：该任务已达到提交限额（上限：{lim_c_text} / {int(lim_b/1024/1024)} MB）。请联系管理员提高阈值或创建新任务。'
                })
                response.headers['Access-Control-Allow-Origin'] = '*'
                response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
                return response, 429

            submission = Submission(task_id=task.id, data=data_text)
            db.add(submission)
            db.commit()
            _invalidate_task_read_cache(task_id)
            _invalidate_task_data_cache(task.id)
        except Exception as e:
            db.rollback()
            logger.exception("保存提交数据失败: %s", e)
            err_text = str(e) or ''
            is_data_too_long = (
                isinstance(e, DataError)
                or 'Data too long for column' in err_text
                or '1406' in err_text
            )
            if is_data_too_long:
                response = jsonify({
                    'error': 'payload_too_large',
                    'message': '提交失败：当前任务的数据字段最大约 60KB，请勿上传图片（尤其是 Base64）。如需传图片，请使用本地版或改为仅提交图片链接 URL。'
                })
                response.headers['Access-Control-Allow-Origin'] = '*'
                response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
                response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
                return response, 413
            response = jsonify({'error': 'save_failed', 'message': MSG_SAVE_FAILED})
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response, 500
        
        response = jsonify({'message': 'Submitted successfully.', 'status': 'success'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Cache-Control'] = 'no-store'
        return response, 200
    except Exception as e:
        logger.exception("submit_form API 异常: %s", e)
        response = jsonify({'error': 'internal_error', 'message': MSG_API_INTERNAL})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 500
    finally:
        db.close()


# 限流日志写入 task.rate_limit_log：MySQL TEXT 仅约 64KB，拼接过长会报 Data too long。
# 迁移后可为 MEDIUMTEXT，仍限制单行体积，只保留尾部（最新记录）。
RATE_LIMIT_LOG_MAX_BYTES = 512 * 1024


def _truncate_utf8_tail(s: str, max_bytes: int) -> str:
    if not s or max_bytes <= 0:
        return s or ''
    b = s.encode('utf-8')
    if len(b) <= max_bytes:
        return s
    b = b[-max_bytes:]
    while b and (b[0] & 0xC0) == 0x80:
        b = b[1:]
    return b.decode('utf-8', errors='ignore')


def _append_rate_limit_log(existing: str, log_entry: str) -> str:
    combined = (existing + '\n' + log_entry) if existing else log_entry
    return _truncate_utf8_tail(combined, RATE_LIMIT_LOG_MAX_BYTES)


def _rate_limit_response(task_id, client_ip, client_fingerprint, ts, db):
    if db:
        task = db.query(Task).filter_by(task_id=task_id).first()
        if task:
            notice = (
                f"设备 {client_fingerprint}（IP {client_ip}）在 {SUBMIT_RATE_LIMIT_WINDOW}s 内多次提交，"
                f"已暂时封禁 {SUBMIT_BLACKLIST_DURATION // 60} 分钟"
            )
            log_entry = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {notice}"
            existing = task.rate_limit_log or ''
            task.rate_limit_log = _append_rate_limit_log(existing, log_entry)
            try:
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"记录限流日志失败: {str(e)}")
 
    response = jsonify({
        'error': 'rate_limit',
        'message': 'Too many requests. Please try again later.',
        'detail': f'提交过于频繁，系统已临时限制该设备约 {SUBMIT_BLACKLIST_DURATION // 60} 分钟。请稍后再试，或降低并发提交频率。'
    })
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response, 429

@quickform_bp.route('/api/<string:task_id>/all', methods=['GET', 'OPTIONS'])
def submit_form_all(task_id):
    """获取任务的全部提交数据"""
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Content-Type'] = 'text/plain; charset=utf-8'
        return response
        
    subject_key, client_fp, client_ip = _all_subject_key(request, task_id)
    now_ts = datetime.utcnow().timestamp()

    with rate_limit_lock:
        info = all_rate_limit_cache.setdefault(subject_key, {'last_access': 0})
        delta = now_ts - float(info.get('last_access', 0) or 0)
        blocked = delta < ALL_RATE_LIMIT_MIN_INTERVAL
        if not blocked:
            info['last_access'] = now_ts
    
    # 限制 /all 接口短间隔访问（按 设备+任务，避免同出口IP互相影响）
    if blocked:
        logger.warning(
            "设备 %s（IP %s）访问 /all 过快：delta=%.3fs < %.3fs，被限流",
            client_fp, client_ip, delta, ALL_RATE_LIMIT_MIN_INTERVAL
        )
        response = jsonify({
            'error': 'rate_limit',
            'message': f'Too many requests. Minimum interval: {ALL_RATE_LIMIT_MIN_INTERVAL:.2f}s for this endpoint.',
            'detail': f'该接口读取较重，请至少间隔 {ALL_RATE_LIMIT_MIN_INTERVAL:.2f} 秒再请求一次。'
        })
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 429
    
    cache_key = _build_read_cache_key('api_task_all', task_id)
    cached_payload = _cache_read_get(cache_key)
    if cached_payload is not None:
        response = jsonify(cached_payload)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Cache-Control'] = 'no-store'
        return response, 200

    db = SessionLocal()
    try:
        task = db.query(Task).filter_by(task_id=task_id).first()
        if not task:
            response = jsonify({
                'error': 'task_not_found',
                'task_id': task_id,
                'message': f'No task found for task_id "{task_id}".',
            })
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            logger.warning(f"请求失败: 任务不存在 - task_id: {task_id}")
            return response, 404
        if not getattr(task, 'is_active', True):
            response = jsonify({
                'error': 'task_disabled',
                'task_id': task_id,
                'message': 'This task is disabled. Data reads are not allowed.',
            })
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response, 403
        
        # 返回全部数据
        submissions = db.query(Submission).filter_by(task_id=task.id).order_by(Submission.submitted_at.desc()).all()
        data_list = []
        for sub in submissions:
            try:
                # 尝试解析JSON数据
                data = json.loads(sub.data)
                # 如果解析后是字符串，可能是双重编码，再解析一次
                if isinstance(data, str):
                    try:
                        data = json.loads(data)
                    except:
                        pass
                data['submitted_at'] = sub.submitted_at.strftime('%Y-%m-%d %H:%M:%S')
                data_list.append(data)
            except (json.JSONDecodeError, TypeError):
                # 如果解析失败，返回原始数据作为raw_data
                data_list.append({
                    'submitted_at': sub.submitted_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'raw_data': sub.data
                })
        
        total_count = len(data_list)
        response = jsonify({
            'note': f'Total {total_count} submission(s).',
            'task_id': task.task_id,
            'task_title': task.title,
            'total_submissions': total_count,
            'submissions': data_list
        })
        payload_bytes = len(response.get_data())
        base_r, base_b = _get_site_all_quota_defaults(db)
        try:
            upd = db.execute(
                text("""
                    UPDATE task SET
                        api_task_all_count = COALESCE(api_task_all_count, 0) + 1,
                        api_task_all_bytes_total = COALESCE(api_task_all_bytes_total, 0) + :pb
                    WHERE id = :tid
                      AND COALESCE(api_task_all_count, 0) < (:base_r + COALESCE(quota_extra_all_reads, 0))
                      AND COALESCE(api_task_all_bytes_total, 0) + :pb <= (:base_b + COALESCE(quota_extra_all_bytes, 0))
                """),
                {
                    "tid": task.id,
                    "pb": payload_bytes,
                    "base_r": base_r,
                    "base_b": base_b,
                },
            )
            rc = getattr(upd, "rowcount", None)
            if rc == 0:
                db.rollback()
                row = db.execute(
                    text(
                        "SELECT COALESCE(api_task_all_count,0), COALESCE(api_task_all_bytes_total,0), "
                        "COALESCE(quota_extra_all_reads,0), COALESCE(quota_extra_all_bytes,0) "
                        "FROM task WHERE id = :id"
                    ),
                    {"id": task.id},
                ).fetchone()
                if row:
                    max_r = base_r + int(row[2] or 0)
                    max_b = base_b + int(row[3] or 0)
                    reasons = []
                    if int(row[0] or 0) >= max_r:
                        reasons.append("读取次数已达上限")
                    if int(row[1] or 0) + payload_bytes > max_b:
                        reasons.append("流量额度不足")
                    detail = "、".join(reasons) if reasons else "配额已满"
                else:
                    detail = "配额已满"
                    max_r = base_r
                    max_b = base_b
                err = jsonify({
                    "error": "quota_exceeded",
                    "message": f"/all 接口：{detail}。请在任务详情页申请加额或联系管理员。",
                    "limits": {
                        "all_reads_max": max_r,
                        "all_bytes_max_mb": max_b // (1024 * 1024),
                    },
                })
                err.headers["Access-Control-Allow-Origin"] = "*"
                err.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
                err.headers["Access-Control-Allow-Headers"] = "Content-Type"
                return err, 429
            _record_api_get("api_task_all")
            db.add(
                ApiAccessLog(
                    task_id=task.id,
                    endpoint="api_task_all",
                    response_bytes=payload_bytes,
                    client_ip=(client_ip or "")[:100],
                )
            )
            db.commit()
            _cache_read_set(cache_key, {
                'note': f'Total {total_count} submission(s).',
                'task_id': task.task_id,
                'task_title': task.title,
                'total_submissions': total_count,
                'submissions': data_list
            })
        except Exception as log_err:
            db.rollback()
            logger.warning("/all 配额或日志写入失败: %s", log_err)
            err = jsonify({"error": "internal_error", "message": MSG_API_INTERNAL})
            err.headers["Access-Control-Allow-Origin"] = "*"
            err.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            err.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return err, 500
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Cache-Control'] = 'no-store'
        return response, 200
    except Exception as e:
        logger.exception("submit_form_all API 异常: %s", e)
        response = jsonify({'error': 'internal_error', 'message': MSG_API_INTERNAL})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 500
    finally:
        db.close()

@quickform_bp.route('/api/tasks', methods=['GET'])
def list_tasks():
    """返回最近的任务列表，便于获取 task_id 进行API测试"""
    db = SessionLocal()
    try:
        tasks = db.query(Task).order_by(Task.created_at.desc()).limit(20).all()
        data = [
            {
                'id': t.id,
                'title': t.title,
                'task_id': t.task_id,
                'created_at': t.created_at.strftime('%Y-%m-%d %H:%M:%S') if t.created_at else ''
            }
            for t in tasks
        ]
        _record_api_get('api_tasks')
        response = jsonify({'items': data, 'count': len(data)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 200
    except Exception as e:
        logger.exception("list_tasks API 异常: %s", e)
        response = jsonify({'error': 'internal_error', 'message': MSG_API_INTERNAL})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 500
    finally:
        db.close()


@quickform_bp.route('/api/stats/overview', methods=['GET', 'OPTIONS'])
def public_stats_overview():
    """运营概览统计（JSON）：返回全站聚合指标（用户数、学校数去重、任务数、提交总数）。

    用途：供运营看板、监控脚本、自建大屏等拉取「整体体量」数据；**不是**单任务明细。
    安全说明：若未配置 STATS_API_TOKEN，则任何知道该 URL 的人均可读取上述汇总数字（不含名单明细）。
    若需限制访问，请在环境变量中设置 STATS_API_TOKEN，请求时携带：查询参数 ?token=...、
    或请求头 X-Stats-Token、或 Authorization: Bearer ...。
    """
    token = (os.getenv('STATS_API_TOKEN') or '').strip()
    if token:
        q = (request.args.get('token') or '').strip()
        hdr = (request.headers.get('X-Stats-Token') or '').strip()
        auth = (request.headers.get('Authorization') or '').strip()
        bearer = ''
        if auth.lower().startswith('bearer '):
            bearer = auth[7:].strip()
        if q != token and hdr != token and bearer != token:
            resp = jsonify({'error': 'unauthorized', 'message': 'Invalid or missing token'})
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Stats-Token'
            return resp, 401

    db = SessionLocal()
    try:
        total_users = db.query(User).count()
        total_tasks = db.query(Task).count()
        total_submissions = db.query(Submission).count()
        # 学校数：与后台统计口径接近（非空、长度>=2、排除常见占位），在库内去重
        school_count = (
            db.query(func.count(func.distinct(User.school)))
            .filter(
                User.school.isnot(None),
                User.school != '',
                func.length(func.trim(User.school)) >= 2,
                User.school.notin_(['xx', '1', 'wkg']),
            )
            .scalar()
        ) or 0

        _record_api_get('api_stats_overview')
        response = jsonify({
            'users': total_users,
            'schools': int(school_count),
            'tasks': total_tasks,
            'submissions': total_submissions,
        })
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Stats-Token'
        return response, 200
    except Exception as e:
        logger.exception('public_stats_overview failed: %s', e)
        response = jsonify({'error': 'internal_error', 'message': 'Internal server error.'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Stats-Token'
        return response, 500
    finally:
        db.close()


@quickform_bp.route('/export/<int:task_id>')
@login_required
def export_data(task_id):
    """导出数据，支持 xls、csv、json 三种格式"""
    fmt = (request.args.get('fmt') or 'xls').lower().strip()
    if fmt not in ('xls', 'xlsx', 'csv', 'json'):
        fmt = 'xls'
    
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        # 权限检查：管理员、任务所有者、组织成员、被共享者可以导出数据
        has_access = False
        if current_user.is_admin() or task.user_id == current_user.id:
            has_access = True
        elif task.organization_id:
            # 检查是否是组织成员
            is_org_member = db.query(OrganizationMember).filter_by(
                organization_id=task.organization_id,
                user_id=current_user.id
            ).first() is not None
            if is_org_member:
                has_access = True
        else:
            # 检查是否被共享
            is_shared = db.query(TaskShare).filter_by(
                task_id=task.id,
                user_id=current_user.id
            ).first() is not None
            if is_shared:
                has_access = True
        
        if not has_access:
            flash('无权访问此数据', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        submission = db.query(Submission).filter_by(task_id=task.id).all()
        
        if not submission:
            flash('没有可导出的数据', 'info')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        data_list = []
        for sub in submission:
            try:
                data = json.loads(sub.data)
                data['submitted_at'] = sub.submitted_at.strftime('%Y-%m-%d %H:%M:%S')
                data_list.append(data)
            except:
                data_list.append({
                    'submitted_at': sub.submitted_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'raw_data': sub.data
                })
        
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        if fmt == 'json':
            # JSON 格式，便于数据大屏使用
            output = io.BytesIO()
            output.write(json.dumps(data_list, ensure_ascii=False, indent=2).encode('utf-8'))
            output.seek(0)
            filename = f"{task.title}_数据导出_{ts}.json"
            try:
                return send_file(output, download_name=filename, as_attachment=True, mimetype='application/json; charset=utf-8')
            except TypeError:
                return send_file(output, attachment_filename=filename, as_attachment=True, mimetype='application/json; charset=utf-8')
        
        elif fmt == 'csv':
            # CSV 格式
            df = pd.DataFrame(data_list)
            output = io.BytesIO()
            df.to_csv(output, index=False, encoding='utf-8-sig')
            output.seek(0)
            filename = f"{task.title}_数据导出_{ts}.csv"
            try:
                return send_file(output, download_name=filename, as_attachment=True, mimetype='text/csv; charset=utf-8')
            except TypeError:
                return send_file(output, attachment_filename=filename, as_attachment=True, mimetype='text/csv; charset=utf-8')
        
        else:
            # XLS/XLSX 格式（默认）
            df = pd.DataFrame(data_list)
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='提交数据')
            output.seek(0)
            filename = f"{task.title}_数据导出_{ts}.xlsx"
            try:
                return send_file(output, download_name=filename, as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            except TypeError:
                return send_file(output, attachment_filename=filename, as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        logger.exception("导出数据失败: %s", e)
        flash('导出失败，请稍后重试。若文件较大可改选 CSV 或缩小日期范围。', 'danger')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    finally:
        db.close()


# ---------- 任务迁移（导出 ZIP / 导入 JSON 或 ZIP）----------
# 会议前临时关闭：改为 True 后恢复导出/导入（实现保留在 _task_migration_*_impl 中）。
TASK_MIGRATION_ACTIVE = False
TASK_MIGRATION_MANIFEST = 'quickform-task-migration.json'
TASK_MIGRATION_DISABLED_FLASH = (
    '任务迁移功能将于 4 月 29 日会议正式发布，敬请期待。会议通知 PDF 可在任务详情页「任务迁移」旁的说明中下载。'
)


def _migration_zip_max_bytes():
    return int(os.getenv('TASK_MIGRATION_ZIP_MAX_BYTES', str(50 * 1024 * 1024)))


def _pick_unique_task_id(db):
    for _ in range(80):
        tid = _generate_task_id()
        if not db.query(Task.id).filter(Task.task_id == tid).first():
            return tid
    raise RuntimeError('无法生成唯一任务 API ID')


def _migration_resolve_html_absolute_path(task, saved_name):
    if not saved_name:
        return None
    bn = os.path.basename(str(saved_name).replace('\\', '/'))
    if not bn or bn in ('.', '..'):
        return None
    candidates = []
    if getattr(task, 'file_path', None):
        try:
            tf = (task.file_path or '').strip()
            if tf and os.path.basename(tf) == bn and os.path.isfile(tf):
                candidates.append(tf)
        except Exception:
            pass
    try:
        candidates.append(os.path.join(_static_uploads_dir(), bn))
    except RuntimeError:
        pass
    candidates.append(os.path.join(STATIC_UPLOADS, bn))
    candidates.append(os.path.join(UPLOAD_FOLDER, bn))
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _migration_collect_html_files(task):
    rows = []
    html_files = []
    if getattr(task, 'html_files', None):
        try:
            html_files = json.loads(task.html_files)
        except Exception:
            html_files = []
    if not isinstance(html_files, list):
        html_files = []
    saved = None
    if task.file_path:
        saved = os.path.basename(task.file_path)
    if task.file_name and saved and not html_files:
        html_files = [{'original_name': task.file_name, 'saved_name': saved}]
    for i, f in enumerate(html_files):
        if not isinstance(f, dict):
            continue
        sn = f.get('saved_name')
        on = f.get('original_name') or sn or ('file_%s.html' % i)
        ap = _migration_resolve_html_absolute_path(task, sn)
        if ap:
            rows.append({'original_name': on, 'abs_path': ap})
    return rows


def _rewrite_html_migration_endpoints(html, old_api_id, new_api_id, old_base='', new_base=''):
    """将 HTML 中的数据接口从旧 APIID（及可选的旧站点根地址）改写为新的。"""
    if not html or not old_api_id or not new_api_id:
        return html
    out = html
    ob = (old_base or '').strip().rstrip('/')
    nb = (new_base or '').strip().rstrip('/')
    if ob and nb and ob != nb:
        s_old = '%s/api/%s' % (ob, old_api_id)
        s_new = '%s/api/%s' % (nb, new_api_id)
        out = out.replace(s_old + '/', s_new + '/')
        out = out.replace(s_old, s_new)
    out = out.replace('/api/%s/' % old_api_id, '/api/%s/' % new_api_id)
    out = out.replace('/api/%s' % old_api_id, '/api/%s' % new_api_id)
    return out


def _migration_validate_zip(zf):
    total = 0
    limit = _migration_zip_max_bytes()
    for info in zf.infolist():
        total += int(getattr(info, 'file_size', 0) or 0)
        if total > limit:
            return False, '迁移包解压后体积过大，请拆分或联系管理员提升 TASK_MIGRATION_ZIP_MAX_BYTES'
        name = (info.filename or '').replace('\\', '/')
        if name.startswith('/') or '..' in name.split('/'):
            return False, '迁移包内存在非法路径'
    return True, None


@quickform_bp.route('/release-announcement/document', methods=['GET'])
@login_required
def meeting_notice_download():
    """会议通知 PDF（URL 不显式暴露服务器 docs/ 物理路径）。"""
    if not os.path.isfile(MEETING_NOTICE_PDF_PATH):
        abort(404)
    return send_file(
        MEETING_NOTICE_PDF_PATH,
        as_attachment=True,
        download_name='会议通知.pdf',
        mimetype='application/pdf',
        max_age=0,
    )


def _task_migration_export_impl(task_id):
    """完整导出实现（ZIP + manifest + HTML）。会议前由 TASK_MIGRATION_ACTIVE=False 短路，此处代码保留无需注释。"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        if not (current_user.is_admin() or task.user_id == current_user.id):
            flash('无权导出该任务模板', 'danger')
            return redirect(url_for('quickform.dashboard'))

        html_rows = _migration_collect_html_files(task)
        export_base = ''
        try:
            export_base = (request.url_root or '').strip()
        except Exception:
            export_base = ''

        manifest = {
            'template_version': 2,
            'exported_at': datetime.now().isoformat(timespec='seconds'),
            'title': (task.title or '').strip(),
            'api_id': (task.task_id or '').strip(),
            'description': (task.description or '').strip(),
            'share_url': (getattr(task, 'share_url', None) or '') or '',
            'tutorial_link': (getattr(task, 'tutorial_link', None) or '') or '',
            'export_api_base': export_base,
            'html_files': [],
        }

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for idx, row in enumerate(html_rows):
                orig = row['original_name'] or ('file_%s.html' % idx)
                ext = ''
                if '.' in orig:
                    ext = orig.rsplit('.', 1)[-1].lower().strip()
                if ext not in ('html', 'htm'):
                    ext = 'html'
                arc = 'html/%s.%s' % (idx, ext)
                try:
                    with open(row['abs_path'], 'rb') as fh:
                        zf.writestr(arc, fh.read())
                except OSError:
                    logger.warning('迁移导出跳过缺失文件: %s', row.get('abs_path'))
                    continue
                manifest['html_files'].append({'original_name': orig, 'archive_name': arc})

            zf.writestr(
                TASK_MIGRATION_MANIFEST,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'),
            )

        buf.seek(0)
        safe_title = re.sub(r'[^0-9A-Za-z\u4e00-\u9fa5_-]+', '_', manifest['title'])[:40] or 'task'
        dl_name = '%s_%s_migration.zip' % (safe_title, manifest['api_id'] or 'export')
        return send_file(
            buf,
            as_attachment=True,
            download_name=dl_name,
            mimetype='application/zip',
            max_age=0,
        )
    except Exception:
        logger.exception('导出任务迁移包失败 task_id=%s', task_id)
        flash('导出失败，请稍后重试', 'danger')
        return redirect(url_for('quickform.dashboard'))
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/export_template', methods=['GET'])
@login_required
def export_task_template(task_id):
    """任务迁移导出路由：会议前仅提示；会议后将 TASK_MIGRATION_ACTIVE=True。"""
    if not TASK_MIGRATION_ACTIVE:
        flash(TASK_MIGRATION_DISABLED_FLASH, 'info')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    return _task_migration_export_impl(task_id)


def _task_migration_import_impl():
    """完整导入实现（JSON v1 / ZIP v2）。会议前由 TASK_MIGRATION_ACTIVE=False 短路，此处代码保留无需注释。"""
    next_url = request.form.get('next') or request.referrer or url_for('quickform.dashboard')
    db = SessionLocal()
    try:
        upload = request.files.get('template_file')
        if not upload or not upload.filename:
            flash('请选择要导入的模板文件（.json 或 .zip）', 'warning')
            return redirect(next_url)

        raw = upload.read()
        fname = (upload.filename or '').strip().lower()
        max_json = int(os.getenv('TASK_TEMPLATE_MAX_BYTES', str(1024 * 1024)))
        max_zip = _migration_zip_max_bytes()

        task_count = db.query(Task).filter_by(user_id=current_user.id).count()
        if not current_user.is_admin():
            if not current_user.can_create_task(SessionLocal, Task):
                task_limit = current_user.task_limit if current_user.task_limit != -1 else '无限制'
                flash(
                    '您已达到任务数量上限（%s个）。如需导入为新任务，请先删除部分任务或申请提升上限。' % task_limit,
                    'warning',
                )
                return redirect(next_url)
            block = _email_requirement_block_for_next_task(db, current_user, task_count)
            if block == 'bind_email':
                flash('创建或导入新任务前请先在个人资料中绑定邮箱。', 'warning')
                return redirect(url_for('quickform.profile', next=next_url))
            if block == 'verify_email':
                flash('创建或导入新任务前请先完成邮箱验证。', 'warning')
                return redirect(url_for('quickform.verify_email', next=next_url))

        rewrite_host = request.form.get('migration_rewrite_host') == '1'
        old_base_in = (request.form.get('migration_old_api_base') or '').strip().rstrip('/')
        new_base_in = (request.form.get('migration_new_api_base') or '').strip().rstrip('/')
        try:
            default_new_base = (request.url_root or '').strip().rstrip('/')
        except Exception:
            default_new_base = ''
        new_base = new_base_in or default_new_base

        # ---------- ZIP（v2）----------
        if fname.endswith('.zip'):
            if len(raw) > max_zip:
                flash('迁移包过大，请控制在环境变量 TASK_MIGRATION_ZIP_MAX_BYTES 限制内', 'warning')
                return redirect(next_url)
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
            except zipfile.BadZipFile:
                flash('无效的 ZIP 迁移包', 'danger')
                return redirect(next_url)
            ok, err = _migration_validate_zip(zf)
            if not ok:
                flash(err or '迁移包校验失败', 'danger')
                zf.close()
                return redirect(next_url)
            try:
                man_raw = zf.read(TASK_MIGRATION_MANIFEST)
            except KeyError:
                zf.close()
                flash('ZIP 中缺少 %s，请使用本站「导出任务」生成的迁移包' % TASK_MIGRATION_MANIFEST, 'danger')
                return redirect(next_url)
            try:
                data = json.loads(man_raw.decode('utf-8'))
            except Exception:
                zf.close()
                flash('迁移清单 JSON 解析失败', 'danger')
                return redirect(next_url)
            zf.close()

            if int(data.get('template_version') or 0) < 2:
                flash('迁移包版本过低或损坏', 'danger')
                return redirect(next_url)

            title = (data.get('title') or '').strip()
            description = (data.get('description') or '').strip()
            old_api_id = (data.get('api_id') or '').strip()
            share_url = (data.get('share_url') or '').strip() or None
            tutorial_link = (data.get('tutorial_link') or '').strip() or None
            if share_url and len(share_url) > 500:
                share_url = share_url[:500]
            if tutorial_link and len(tutorial_link) > 500:
                tutorial_link = tutorial_link[:500]

            if not title:
                flash('迁移包缺少任务标题', 'warning')
                return redirect(next_url)
            if len(title) > 200:
                flash('任务标题过长', 'warning')
                return redirect(next_url)
            if not old_api_id:
                flash('迁移包缺少原 API ID 信息', 'warning')
                return redirect(next_url)
            if description and len(description) > 20000:
                flash('任务描述过长，请精简源任务后重新导出', 'warning')
                return redirect(next_url)

            ob = old_base_in
            if rewrite_host and not ob:
                ob = (data.get('export_api_base') or '').strip().rstrip('/')
            nb = new_base

            try:
                new_api_id = _pick_unique_task_id(db)
            except RuntimeError:
                flash('无法分配新的 API ID，请稍后重试', 'danger')
                return redirect(next_url)

            zf = zipfile.ZipFile(io.BytesIO(raw))
            static_uploads = _static_uploads_dir()
            stored_list = []
            try:
                for entry in data.get('html_files') or []:
                    if not isinstance(entry, dict):
                        continue
                    arc = entry.get('archive_name')
                    oname = (entry.get('original_name') or 'page.html').strip() or 'page.html'
                    if not isinstance(arc, str) or not arc:
                        continue
                    arc_norm = arc.replace('\\', '/')
                    if arc_norm.startswith('/') or '..' in arc_norm.split('/'):
                        continue
                    try:
                        body = zf.read(arc_norm)
                    except KeyError:
                        continue
                    try:
                        text = body.decode('utf-8')
                    except UnicodeDecodeError:
                        text = body.decode('utf-8', errors='replace')

                    if rewrite_host and ob and nb:
                        text = _rewrite_html_migration_endpoints(text, old_api_id, new_api_id, ob, nb)
                    else:
                        text = _rewrite_html_migration_endpoints(text, old_api_id, new_api_id, '', '')

                    if len(text.encode('utf-8')) > MAX_HTML_FILE_SIZE:
                        flash('某个 HTML 超过单文件大小限制（4MB），请压缩后重新导出', 'warning')
                        return redirect(next_url)

                    bio = io.BytesIO(text.encode('utf-8'))
                    fs = FileStorage(stream=bio, filename=oname)
                    unique_filename, filepath = save_uploaded_file(fs, static_uploads, ALLOWED_EXTENSIONS)
                    if not unique_filename or not filepath:
                        flash('保存迁移 HTML 失败', 'danger')
                        return redirect(next_url)
                    stored_list.append({'original_name': oname, 'saved_name': unique_filename})
            finally:
                zf.close()

            if data.get('html_files') and not stored_list:
                flash('迁移包声明了 HTML 文件但均未成功解压或保存，请检查包是否完整', 'danger')
                return redirect(next_url)

            new_task = Task(
                title=title,
                description=description or None,
                user_id=current_user.id,
                task_id=new_api_id,
                sharing_type='private',
                organization_id=None,
                file_name=None,
                file_path=None,
                html_files=None,
                share_url=share_url,
                tutorial_link=tutorial_link,
            )
            if stored_list:
                new_task.html_files = json.dumps(stored_list, ensure_ascii=False)
                new_task.file_name = stored_list[0]['original_name']
                new_task.file_path = os.path.join(static_uploads, stored_list[0]['saved_name'])
                if new_task.file_path and new_task.file_path.lower().endswith(('.html', '.htm')):
                    if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                        new_task.html_approved = 1
                        new_task.html_approved_by = current_user.id
                        new_task.html_approved_at = datetime.now()
                        new_task.html_review_note = None
                    else:
                        new_task.html_approved = 0
                        new_task.html_approved_by = None
                        new_task.html_approved_at = None
                        new_task.html_review_note = None

            db.add(new_task)
            db.commit()
            if new_task.file_path and str(new_task.file_path).lower().endswith(('.html', '.htm')):
                try:
                    analyze_html_file(
                        new_task.id,
                        current_user.id,
                        new_task.file_path,
                        SessionLocal,
                        Task,
                        AIConfig,
                        read_file_content,
                        call_ai_model,
                    )
                except Exception as ex:
                    logger.warning('迁移导入后自动分析 HTML 未启动: %s', ex)
            flash('任务迁移导入成功（已分配新的数据接口 APIID，HTML 已按选项改写）', 'success')
            return redirect(url_for('quickform.task_detail', task_id=new_task.id))

        # ---------- JSON（v1）----------
        if not fname.endswith('.json'):
            flash('请上传 .json 模板或 .zip 迁移包', 'warning')
            return redirect(next_url)
        if len(raw) > max_json:
            flash('模板文件过大，请控制在 1MB 以内', 'warning')
            return redirect(next_url)
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception:
            flash('模板文件解析失败，请检查 JSON 格式', 'danger')
            return redirect(next_url)

        title = (data.get('title') or '').strip()
        api_id = (data.get('api_id') or data.get('task_id') or '').strip()
        description = (data.get('description') or '').strip()
        if not title or not api_id:
            flash('模板缺少必要字段：任务名称或 APIID', 'warning')
            return redirect(next_url)
        if len(title) > 200 or len(api_id) > 50:
            flash('任务名称或 APIID 超出长度限制', 'warning')
            return redirect(next_url)
        if description and len(description) > 20000:
            flash('任务描述过长，请精简后重试', 'warning')
            return redirect(next_url)

        if db.query(Task.id).filter(Task.task_id == api_id).first():
            flash('服务器已存在相同 APIID 的任务，已禁止重复导入', 'warning')
            return redirect(next_url)

        new_task = Task(
            title=title,
            description=description or None,
            user_id=current_user.id,
            task_id=api_id,
            sharing_type='private',
            organization_id=None,
            file_name=None,
            file_path=None,
            html_files=None,
        )
        db.add(new_task)
        db.commit()
        flash('任务模板导入成功（不含 HTML，可随后在编辑页上传）', 'success')
        return redirect(url_for('quickform.task_detail', task_id=new_task.id))
    except IntegrityError:
        db.rollback()
        flash('导入失败：任务标识冲突，请更换模板后重试', 'danger')
        return redirect(next_url)
    except Exception:
        db.rollback()
        logger.exception('导入任务模板失败')
        flash('导入失败，请稍后重试', 'danger')
        return redirect(next_url)
    finally:
        db.close()


@quickform_bp.route('/task/import_template', methods=['POST'])
@login_required
def import_task_template():
    """任务迁移导入路由：会议前仅提示；会议后将 TASK_MIGRATION_ACTIVE=True。"""
    next_url = request.form.get('next') or request.referrer or url_for('quickform.dashboard')
    if not TASK_MIGRATION_ACTIVE:
        flash(TASK_MIGRATION_DISABLED_FLASH, 'info')
        return redirect(next_url)
    return _task_migration_import_impl()


@quickform_bp.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    """个人设置"""
    db = SessionLocal()
    try:
        ai_config = db.query(AIConfig).filter_by(user_id=current_user.id).first()
        decrypt_ai_config_inplace(ai_config)
        user_record = db.get(User, current_user.id)
        pending_cert_request = db.query(CertificationRequest).filter_by(user_id=current_user.id, status=0).order_by(CertificationRequest.created_at.desc()).first()
        last_cert_request = db.query(CertificationRequest).filter_by(user_id=current_user.id).order_by(CertificationRequest.created_at.desc()).first()
        
        if request.method == 'POST':
            # 恢复默认：设为「空」即使用系统/管理员配置的 API（如硅基流动 Token），方便老师先试用
            if 'reset_config' in request.form:
                if not ai_config:
                    ai_config = AIConfig(user_id=current_user.id)
                    db.add(ai_config)
                    db.flush()

                # 默认使用 chat_server（硅基流动），所有 Key/URL 恢复为空，调用时回退到环境变量/系统配置的 API
                ai_config.selected_model = 'chat_server'
                ai_config.chat_server_api_url = ''
                ai_config.chat_server_api_token = ''
                ai_config.deepseek_api_key = ''
                ai_config.doubao_api_key = ''
                ai_config.doubao_secret_key = ''
                ai_config.qwen_api_key = ''
                ai_config.moonshot_api_key = ''
                ai_config.glm_api_key = ''
                ai_config.ernie_api_key = ''
                ai_config.ernie_secret_key = ''
                ai_config.openrouter_api_key = ''

                encrypt_ai_config_inplace(ai_config)
                db.commit()
                flash('已恢复为空，将使用系统默认 API（管理员配置）供您试用；如需用自己的密钥请在下方填写后保存。', 'success')
                return redirect(url_for('quickform.profile') + '#config')

            # 更新 AI 配置（保存配置按钮提交，与「恢复默认」为不同表单，不会同时带 reset_config）
            if 'selected_model' in request.form:
                selected_model = request.form.get('selected_model')
                deepseek_api_key = request.form.get('deepseek_api_key', '')
                doubao_api_key = request.form.get('doubao_api_key', '')
                qwen_api_key = request.form.get('qwen_api_key', '')
                chat_server_api_url = request.form.get('chat_server_api_url', '')
                chat_server_api_token = request.form.get('chat_server_api_token', '')
                moonshot_api_key = request.form.get('moonshot_api_key', '')
                glm_api_key = request.form.get('glm_api_key', '')
                ernie_api_key = request.form.get('ernie_api_key', '')
                ernie_secret_key = request.form.get('ernie_secret_key', '')
                openrouter_api_key = request.form.get('openrouter_api_key', '')
                
                if ai_config:
                    ai_config.selected_model = selected_model
                    ai_config.deepseek_api_key = deepseek_api_key
                    ai_config.doubao_api_key = doubao_api_key
                    ai_config.qwen_api_key = qwen_api_key
                    ai_config.chat_server_api_url = chat_server_api_url
                    ai_config.chat_server_api_token = chat_server_api_token
                    ai_config.moonshot_api_key = moonshot_api_key
                    ai_config.glm_api_key = glm_api_key
                    ai_config.ernie_api_key = ernie_api_key
                    ai_config.ernie_secret_key = ernie_secret_key
                    ai_config.openrouter_api_key = openrouter_api_key
                else:
                    ai_config = AIConfig(
                        user_id=current_user.id,
                        selected_model=selected_model,
                        deepseek_api_key=deepseek_api_key,
                        doubao_api_key=doubao_api_key,
                        qwen_api_key=qwen_api_key,
                        chat_server_api_url=chat_server_api_url,
                        chat_server_api_token=chat_server_api_token,
                        moonshot_api_key=moonshot_api_key,
                        glm_api_key=glm_api_key,
                        ernie_api_key=ernie_api_key,
                        ernie_secret_key=ernie_secret_key,
                        openrouter_api_key=openrouter_api_key
                    )
                    db.add(ai_config)
                encrypt_ai_config_inplace(ai_config)
                db.commit()
                flash('AI配置更新成功', 'success')
                return redirect(url_for('quickform.profile') + '#config')
            
            elif 'current_password' in request.form:
                current_password = request.form.get('current_password')
                new_password = request.form.get('new_password')
                confirm_password = request.form.get('confirm_password', '')
                
                if not bcrypt.check_password_hash(current_user.password, current_password):
                    flash('当前密码错误', 'danger')
                elif new_password != confirm_password:
                    flash('新密码与确认密码不匹配', 'danger')
                elif len(new_password) < 6:
                    flash('密码长度至少为6个字符', 'danger')
                else:
                    hashed = bcrypt.generate_password_hash(new_password).decode('utf-8')
                    current_user.password = hashed
                    if user_record:
                        user_record.password = hashed
                    db.commit()
                    flash('密码修改成功', 'success')
            
            elif 'update_profile' in request.form:
                # 修改个人信息
                username = request.form.get('username', '').strip()
                email = request.form.get('email', '').strip()
                school = request.form.get('school', '').strip()
                phone = request.form.get('phone', '').strip()
                
                if not username or not email or not school or not phone:
                    flash('请填写所有必填字段', 'danger')
                    return redirect(url_for('quickform.profile'))
                
                import re
                if not re.match(r'^1[3-9]\d{9}$', phone):
                    flash('请输入正确的11位手机号码', 'danger')
                    return redirect(url_for('quickform.profile'))
                
                # 检查用户名和邮箱是否已被其他用户使用
                existing_user = db.query(User).filter(
                    (User.username == username) | (User.email == email)
                ).filter(User.id != current_user.id).first()
                
                if existing_user:
                    flash('用户名或邮箱已被其他用户使用', 'danger')
                    return redirect(url_for('quickform.profile'))
                
                # 更新用户信息
                if user_record:
                    user_record.username = username
                    user_record.email = email
                    user_record.school = school
                    user_record.phone = phone
                else:
                    current_user.username = username
                    current_user.email = email
                    current_user.school = school
                    current_user.phone = phone
                
                db.commit()
                flash('个人信息更新成功', 'success')
            
            return redirect(url_for('quickform.profile'))
        
        u = user_record or current_user
        email_val = (u.email or '').strip()
        email_is_placeholder = _is_placeholder_or_empty_email(email_val)
        email_display = '' if email_is_placeholder else email_val
        return render_template(
            'profile.html',
            user=u,
            ai_config=ai_config,
            pending_cert_request=pending_cert_request,
            last_cert_request=last_cert_request,
            email_is_placeholder=email_is_placeholder,
            email_display=email_display
        )
    finally:
        db.close()

@quickform_bp.route('/certification/request', methods=['GET', 'POST'])
@login_required
def certification_request():
    """教师认证申请"""
    db = SessionLocal()
    try:
        user = db.get(User, current_user.id)
        if not user:
            flash('用户不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))

        pending_request = db.query(CertificationRequest).filter_by(user_id=user.id, status=0).order_by(CertificationRequest.created_at.desc()).first()
        requests = db.query(CertificationRequest).filter_by(user_id=user.id).order_by(CertificationRequest.created_at.desc()).all()

        if request.method == 'POST':
            if user.is_certified:
                flash('您已完成认证，无需重复提交。', 'info')
                return redirect(url_for('quickform.profile'))
            if pending_request:
                flash('您已有待审核的认证申请，请耐心等待结果。', 'warning')
                return redirect(url_for('quickform.certification_request'))

            file = request.files.get('certificate_file')
            if not file or not file.filename.strip():
                flash('请上传能够证明教师身份的材料（允许图片或PDF）。', 'danger')
                return redirect(url_for('quickform.certification_request'))

            unique_filename, filepath = save_uploaded_file(file, CERTIFICATION_FOLDER, CERTIFICATION_ALLOWED_EXTENSIONS)
            if not unique_filename:
                flash('文件上传失败或格式不支持，请重试。支持 PNG / JPG / JPEG / PDF 格式。', 'danger')
                return redirect(url_for('quickform.certification_request'))

            cert_request = CertificationRequest(
                user_id=user.id,
                file_name=file.filename,
                file_path=filepath,
                status=0,
                created_at=datetime.now()
            )
            db.add(cert_request)
            db.commit()
            flash('认证申请已提交，请等待管理员审核。', 'success')
            return redirect(url_for('quickform.profile'))

        return render_template('certification_request.html', user=user, requests=requests, pending_request=pending_request)
    finally:
        db.close()


@quickform_bp.route('/certification/file/<int:request_id>')
@login_required
def user_view_certification_file(request_id):
    """
    教师查看自己提交的认证材料（图片或 PDF）。
    仅限提交该申请的用户或管理员访问。
    """
    db = SessionLocal()
    try:
        cert_request = db.get(CertificationRequest, request_id)
        if not cert_request:
            flash('认证申请不存在', 'danger')
            return redirect(request.referrer or url_for('quickform.certification_request'))

        if (cert_request.user_id != current_user.id) and (not current_user.is_admin()):
            flash('无权查看该认证材料', 'danger')
            return redirect(request.referrer or url_for('quickform.certification_request'))

        if not cert_request.file_path or not os.path.exists(cert_request.file_path):
            flash('认证材料文件不存在', 'danger')
            return redirect(request.referrer or url_for('quickform.certification_request'))

        directory, filename = os.path.split(cert_request.file_path)
        return send_from_directory(directory, filename)
    finally:
        db.close()

# ai_service 已集成支持的模型（moonshot/glm/ernie/openrouter 待后续集成）
SUPPORTED_AI_MODELS = {'chat_server', 'deepseek', 'doubao', 'qwen'}


def _config_from_payload(cfg):
    """从前端提交的 config 字典构建用于 call_ai_model 的配置对象（与 AIConfig 属性一致）"""
    from types import SimpleNamespace
    return SimpleNamespace(
        selected_model=(cfg.get('selected_model') or '').strip(),
        deepseek_api_key=(cfg.get('deepseek_api_key') or '').strip(),
        doubao_api_key=(cfg.get('doubao_api_key') or '').strip(),
        doubao_secret_key=(cfg.get('doubao_secret_key') or '').strip(),
        qwen_api_key=(cfg.get('qwen_api_key') or '').strip(),
        chat_server_api_url=(cfg.get('chat_server_api_url') or '').strip(),
        chat_server_api_token=(cfg.get('chat_server_api_token') or '').strip(),
    )


@quickform_bp.route('/api/test_ai', methods=['POST'])
@login_required
def test_ai_api():
    """测试当前用户的AI配置是否可用；若请求体带 config，则用当前表单配置测试（未保存也可测）"""
    db = SessionLocal()
    try:
        payload = request.get_json(silent=True) or {}
        cfg_payload = payload.get('config')
        if cfg_payload and isinstance(cfg_payload, dict) and (cfg_payload.get('selected_model') or '').strip():
            ai_config = _config_from_payload(cfg_payload)
        else:
            ai_config = db.query(AIConfig).filter_by(user_id=current_user.id).first()
            decrypt_ai_config_inplace(ai_config)
            if not ai_config or not ai_config.selected_model:
                return jsonify({'success': False, 'message': '请先保存AI配置后再测试，或在上方选择模型并填写密钥后直接点击测试'}), 400

        if ai_config.selected_model not in SUPPORTED_AI_MODELS:
            model_label = MODEL_LABELS.get(ai_config.selected_model, ai_config.selected_model)
            return jsonify({'success': False, 'message': f'{model_label} 暂未集成，敬请期待后续版本'}), 400

        test_prompt = (payload.get('prompt') or '这是一次连通性测试，请简短回复“OK”。').strip()
        if not test_prompt:
            test_prompt = '这是一次连通性测试，请简短回复“OK”。'

        try:
            response_text = call_ai_model(test_prompt, ai_config)
        except Exception as e:
            logger.exception("AI配置测试失败: %s", e)
            friendly = _to_user_friendly_ai_error(str(e))
            return jsonify({'success': False, 'message': friendly}), 200

        preview = (response_text or '').strip()
        if len(preview) > 200:
            preview = preview[:200] + '...'

        model_label = MODEL_LABELS.get(ai_config.selected_model, ai_config.selected_model)
        return jsonify({
            'success': True,
            'message': '调用成功，请确认响应内容是否符合预期',
            'model': ai_config.selected_model,
            'model_label': model_label,
            'response_preview': preview
        })
    finally:
        db.close()

@quickform_bp.route('/analyze/<int:task_id>/smart_analyze', methods=['GET', 'POST'])
@login_required
def smart_analyze(task_id):
    """智能分析"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        # 权限检查：管理员、任务所有者、被共享者、组织成员可以生成分析报告
        has_access = False
        if current_user.is_admin() or task.user_id == current_user.id:
            has_access = True
        elif db.query(TaskShare).filter_by(
            task_id=task.id,
            user_id=current_user.id
        ).first():
            has_access = True
        elif task.organization_id:
            is_org_member = db.query(OrganizationMember).filter_by(
                organization_id=task.organization_id,
                user_id=current_user.id
            ).first() is not None
            if is_org_member:
                has_access = True
        
        if not has_access:
            flash('无权访问此任务', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        ai_config = db.query(AIConfig).filter_by(user_id=current_user.id).first()
        decrypt_ai_config_inplace(ai_config)
        model_label = None
        ai_ready = False
        ai_ready_reason = ''
        if ai_config and ai_config.selected_model:
            model_label = MODEL_LABELS.get(ai_config.selected_model, ai_config.selected_model)
            if ai_config.selected_model not in SUPPORTED_AI_MODELS:
                ai_ready_reason = f"{model_label} 暂未集成，敬请期待后续版本"
            elif ai_config.selected_model == 'deepseek' and not ai_config.deepseek_api_key:
                ai_ready_reason = "请先配置DeepSeek API密钥"
            elif ai_config.selected_model == 'doubao' and not ai_config.doubao_api_key:
                ai_ready_reason = "请先配置豆包API密钥"
            else:
                ai_ready = True
        else:
            ai_ready_reason = "请先在个人设置中配置AI模型和API密钥"

        # 数据概览：供「数据分析向导」使用（尽量从数据库取样，避免 /all 压力）
        submission_q = db.query(Submission).filter_by(task_id=task_id)
        current_submission_count = submission_q.count()
        # 取多条样例数据（默认 3 条），让大屏生成能更好“自适应字段”
        sample_n = 3
        try:
            sample_n_env = int((os.getenv('QF_DASHBOARD_SAMPLE_N') or '').strip() or '3')
            sample_n = max(1, min(8, sample_n_env))
        except Exception:
            sample_n = 3
        sample_rows = (
            submission_q.order_by(Submission.id.asc())
            .limit(sample_n)
            .all()
        )
        samples = []
        for r in (sample_rows or []):
            s = ((getattr(r, 'data', '') or '')).strip()
            if not s:
                continue
            samples.append(s)
        if samples:
            # 多条样例拼成数组展示（更贴近 /all 的返回结构）
            sample_data_raw = "[\n" + ",\n".join(samples) + "\n]"
        else:
            sample_data_raw = ''
        if len(sample_data_raw) > 6000:
            sample_data_raw = sample_data_raw[:6000] + '...'
        api_base_url = _public_site_base_url()
        all_url = f"{api_base_url}/api/{task.task_id}/all"
        stable_dash_saved = f"dash_{task.task_id}.html"
        
        # 如果是提交生成请求，则同步生成并返回同页结果
        if request.method == 'POST':
            form_action = (request.form.get('action') or '').strip()

            # 1. 制作实时数据大屏：自动生成/修改（异步；最多 3 次）
            if form_action in ('dashboard_generate', 'dashboard_revise'):
                if current_submission_count <= 0:
                    flash('你至少需要回收一条数据。', 'warning')
                    return redirect(url_for('quickform.smart_analyze', task_id=task.id))
                if not ai_ready:
                    flash(ai_ready_reason or '请先配置 AI 后再自动生成。', 'warning')
                    return redirect(url_for('quickform.smart_analyze', task_id=task.id))

                remaining = getattr(task, 'dashboard_ai_edit_remaining', None)
                if remaining is None:
                    remaining = 3
                try:
                    remaining = int(remaining)
                except Exception:
                    remaining = 0
                if remaining <= 0:
                    flash('仅允许 3 次自动生成/修改，当前次数已用完。', 'warning')
                    return redirect(url_for('quickform.smart_analyze', task_id=task.id))

                user_prompt = (request.form.get('dashboard_user_prompt') or '').strip()
                revise_ins = (request.form.get('dashboard_revision') or '').strip()
                base_html_upload = request.files.get('dashboard_base_html')
                base_html_text = ''
                if form_action == 'dashboard_generate':
                    # 要求老师上传自己的学生端 HTML 作为“页面基底”
                    if not base_html_upload or not getattr(base_html_upload, 'filename', ''):
                        flash('请先上传你的学生端 HTML 文件（必填）。', 'warning')
                        return redirect(url_for('quickform.smart_analyze', task_id=task.id))
                    try:
                        raw = base_html_upload.read() or b''
                        # 只取一部分内容，避免 token 过长（保留开头+结尾，利于结构/样式/脚本保持一致）
                        max_bytes = 24000
                        head_bytes = raw[:max_bytes]
                        tail_bytes = raw[-8000:] if len(raw) > (max_bytes + 8000) else b''
                        base_html_text = head_bytes.decode('utf-8', errors='ignore')
                        if tail_bytes:
                            base_html_text += "\n\n<!-- (中间内容省略) -->\n\n" + tail_bytes.decode('utf-8', errors='ignore')
                        base_html_text = (base_html_text or '').strip()
                    except Exception:
                        base_html_text = ''

                base_prompt = (
                    "你是一名资深前端工程师。请生成一个单页 HTML（只输出完整 HTML）。\n"
                    "目标：制作一个可以实时分析统计的数据大屏（数据看板）。\n"
                    f"数据来自：{all_url}\n"
                    "要求：每隔 10 秒刷新一次（fetch 获取 JSON），并渲染关键指标与图表。\n"
                    "页面要求：手机端可用、布局清晰、默认浅色主题；不要依赖外部 CDN；所有逻辑写在同一个 HTML 文件里。\n"
                    "重要约束：不要使用 Chart.js / ECharts 等外部库名（例如 Chart、echarts），因为本页面不允许外部 CDN，且未内置这些库；\n"
                    "如需图表，请使用原生 Canvas（2D）自行绘制，或用纯 HTML/CSS 的条形/进度条等方式展示，避免出现“Chart is not defined”等运行错误。\n"
                    "数据格式如下（示例，字段可能更多，请自适应）：\n"
                    f"{sample_data_raw or '[暂无示例数据]'}\n"
                )
                if base_html_text:
                    base_prompt += (
                        "\n现有学生端页面（你需要以此为基础进行改造，尽量保留其样式与表单字段一致性；如下为节选）：\n"
                        + base_html_text + "\n"
                    )
                if user_prompt:
                    base_prompt += "\n用户需求补充：\n" + user_prompt + "\n"
                if form_action == 'dashboard_revise' and revise_ins:
                    base_prompt += "\n在保留现有大屏功能的基础上，按以下说明修改：\n" + revise_ins + "\n"

                task.dashboard_generation_status = 'pending'
                task.dashboard_generation_error = None
                task.dashboard_saved_name = task.dashboard_saved_name or stable_dash_saved
                task.dashboard_ai_edit_remaining = max(0, remaining - 1)
                db.commit()

                def _dash_bg(task_pk: int, ai_cfg_id: int, prompt_text: str, saved_name: str):
                    _db = SessionLocal()
                    try:
                        tsk = _db.get(Task, task_pk)
                        cfg = _db.get(AIConfig, ai_cfg_id)
                        decrypt_ai_config_inplace(cfg)
                        html = call_ai_model(prompt_text, cfg, chat_server_model=get_chat_server_model_light())
                        html = (html or '').strip()
                        if html.startswith("```"):
                            html = html.strip('` \n')
                        static_uploads = _static_uploads_dir()
                        out_path = os.path.join(static_uploads, saved_name)
                        with open(out_path, 'w', encoding='utf-8') as f:
                            f.write(html)
                        tsk.dashboard_file_name = saved_name
                        tsk.dashboard_generated_at = datetime.now()
                        tsk.dashboard_generation_status = 'completed'
                        tsk.dashboard_generation_error = None
                        _db.commit()
                    except Exception as ex:
                        try:
                            tsk = _db.get(Task, task_pk)
                            tsk.dashboard_generation_status = 'failed'
                            friendly = _to_user_friendly_ai_error(str(ex))
                            tsk.dashboard_generation_error = (
                                "自动生成失败：可能是提示词过长（包含学生端 HTML + 多条数据样例）超过模型上限。\n"
                                "兜底方案：请改用「方式1：手动生成」，复制提示词模板到 AI 工具生成数据大屏。\n\n"
                                + friendly
                            ) if '超过模型可处理上限' in friendly else friendly
                            # 如果是“上下文/长度超限”，不消耗次数（退回 1 次）
                            if '超过模型可处理上限' in friendly:
                                try:
                                    cur = getattr(tsk, 'dashboard_ai_edit_remaining', None)
                                    cur = int(cur) if cur is not None else 0
                                    tsk.dashboard_ai_edit_remaining = min(3, cur + 1)
                                except Exception:
                                    pass
                            _db.commit()
                        except Exception:
                            _db.rollback()
                        logger.exception("数据大屏生成失败: %s", ex)
                    finally:
                        _db.close()

                try:
                    t = threading.Thread(
                        target=_dash_bg,
                        args=(task.id, ai_config.id, base_prompt, task.dashboard_saved_name),
                        daemon=True
                    )
                    t.start()
                    return redirect(url_for('quickform.smart_analyze', task_id=task.id, dash_running=1))
                except Exception as e:
                    logger.exception("启动数据大屏生成线程失败: %s", e)
                    flash('无法启动数据大屏生成，请稍后重试。', 'danger')
                    return redirect(url_for('quickform.smart_analyze', task_id=task.id))

            # 检查是仅保存模板还是生成报告；report_action：generate 或 polish_and_generate
            action = request.form.get('action', 'generate')  # 'save_template' 或 'generate'
            report_action = request.form.get('report_action', 'generate')
            
            # 合并输入框：接口描述 + 关注点，统一存到 user_prompt_template（兼容旧字段）
            report_context = (request.form.get('report_context', '') or '').strip()
            if not report_context:
                # 兼容旧版本表单字段
                legacy_interface_desc = (request.form.get('interface_desc', '') or '').strip()
                legacy_focus = (request.form.get('user_prompt_template', '') or '').strip()
                report_context = '\n'.join([x for x in [legacy_interface_desc, legacy_focus] if x]).strip()
            if report_context:
                task.user_prompt_template = report_context
                db.commit()
            
            # 如果只是保存模板，直接返回
            if action == 'save_template':
                flash('提示词模板已保存', 'success')
                return redirect(url_for('quickform.smart_analyze', task_id=task.id))

            # 生成分析报告需要 AI 配置
            if not ai_ready:
                flash(ai_ready_reason or '请先在个人设置中配置 AI 模型与 APIKEY。', 'warning')
                return redirect(url_for('quickform.smart_analyze', task_id=task.id))
            
            # 生成报告的逻辑
            # 获取提交数据（支持数据范围筛选）
            submission_for_prompt = db.query(Submission).filter_by(task_id=task_id).all()
            data_range = request.form.get('data_range', 'all')
            if data_range == 'single_day':
                single_date_str = request.form.get('single_date', '')
                if single_date_str:
                    from datetime import datetime as dt
                    try:
                        target_date = dt.strptime(single_date_str, '%Y-%m-%d').date()
                        submission_for_prompt = [s for s in submission_for_prompt if s.submitted_at and s.submitted_at.date() == target_date]
                    except (ValueError, TypeError):
                        pass
            elif data_range == 'date_range':
                date_start_str = request.form.get('date_start', '')
                date_end_str = request.form.get('date_end', '')
                if date_start_str and date_end_str:
                    from datetime import datetime as dt
                    try:
                        start_d = dt.strptime(date_start_str, '%Y-%m-%d').date()
                        end_d = dt.strptime(date_end_str, '%Y-%m-%d').date()
                        submission_for_prompt = [s for s in submission_for_prompt if s.submitted_at and start_d <= s.submitted_at.date() <= end_d]
                    except (ValueError, TypeError):
                        pass
            # 接口描述：默认用任务简介，表单里不再单独编辑（合并到 report_context）
            interface_desc = (task.description or '').strip()
            file_content_for_prompt = None
            if task.file_path and os.path.exists(task.file_path):
                file_content_for_prompt = read_file_content(task.file_path)
            
            # 若用户在高阶编辑中填写了「完整提示词」，优先使用该内容，否则再根据表单生成
            custom_prompt_from_form = request.form.get('custom_prompt', '').strip()
            if custom_prompt_from_form:
                custom_prompt = custom_prompt_from_form
            else:
                user_prompt_from_form = (request.form.get('report_context', '') or '').strip()
                if not user_prompt_from_form:
                    # 兼容旧字段
                    user_prompt_from_form = (request.form.get('user_prompt_template', '') or '').strip()
                user_template_val = user_prompt_from_form or (task.user_prompt_template if task.user_prompt_template else None)
                custom_prompt = generate_analysis_prompt(
                    task, submission_for_prompt, file_content_for_prompt,
                    SessionLocal, Submission,
                    user_template=user_template_val,
                    interface_desc=interface_desc or None
                )
            
            # 若为「润色提示词并生成报告」，先调用 AI 润色提示词再生成
            if report_action == 'polish_and_generate' and custom_prompt:
                try:
                    polish_prompt = (
                        "请将以下数据分析需求改写成一条更清晰、专业、便于大模型执行的分析提示词。"
                        "只输出润色后的完整提示词内容，不要输出解释或前缀。\n\n" + custom_prompt
                    )
                    polished = call_ai_model(
                        polish_prompt, ai_config, chat_server_model=get_chat_server_model_light()
                    )
                    if polished and polished.strip():
                        custom_prompt = polished
                except Exception as e:
                    logger.warning(f"润色提示词失败，将使用原提示词: {e}")
            
            # 保存完整提示词（用于兼容旧代码）
            # MySQL TEXT 列约 64KB，超长提示词会触发 1406 Data too long。
            prompt_trimmed = False
            custom_prompt_to_save = custom_prompt or ''
            max_prompt_bytes = 60000
            prompt_bytes = custom_prompt_to_save.encode('utf-8', errors='ignore')
            if len(prompt_bytes) > max_prompt_bytes:
                custom_prompt_to_save = prompt_bytes[:max_prompt_bytes].decode('utf-8', errors='ignore')
                prompt_trimmed = True
            task.custom_prompt = custom_prompt_to_save
            try:
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning(f"保存 custom_prompt 失败，改为不落库继续生成: {e}")
                task.custom_prompt = None
                db.commit()
                prompt_trimmed = True
            if prompt_trimmed:
                flash('数据量较大：提示词已自动裁剪后保存，不影响本次报告生成。', 'warning')
            
            try:
                # 后台线程执行，避免阻塞主请求线程
                t = threading.Thread(target=perform_analysis_with_custom_prompt, args=(
                    task_id, current_user.id, ai_config.id, custom_prompt,
                    SessionLocal, Task, Submission, AIConfig,
                    read_file_content, call_ai_model, save_analysis_report,
                    User, OrganizationMember, TaskShare
                ), daemon=True)
                t.start()
                # 跳转到本页并标记运行中，前端据此开始轮询
                return redirect(url_for('quickform.smart_analyze', task_id=task.id, running=1))
            except Exception as e:
                logger.exception("启动报告生成线程失败: %s", e)
                return render_template(
                    'smart_analyze.html',
                    task=task,
                    error='无法启动报告生成，请稍后重试。若持续失败请联系管理员。',
                    ai_config=ai_config,
                    now=datetime.now(),
                    model_label=model_label,
                )
        
        # GET 或 POST 完成后，准备页面所需数据
        # 刷新task对象以获取最新的html_analysis和custom_prompt
        db.refresh(task)
        submission = db.query(Submission).filter_by(task_id=task_id).all()
        current_submission_count = len(submission)
        file_content = None
        if task.file_path and os.path.exists(task.file_path):
            file_content = read_file_content(task.file_path)
        
        # 检查保存的提示词中的数据条数是否与当前实际数据条数一致
        should_regenerate_prompt = False
        if task.custom_prompt and task.custom_prompt.strip():
            # 尝试从提示词中提取数据条数
            # 匹配 "总提交数量：X 条" 或 "共有 X 条提交记录"
            count_patterns = [
                r'总提交数量[：:]\s*(\d+)\s*条',
                r'共有\s*(\d+)\s*条提交记录',
                r'总提交数量[：:]\s*(\d+)',
            ]
            saved_count = None
            for pattern in count_patterns:
                match = re.search(pattern, task.custom_prompt)
                if match:
                    saved_count = int(match.group(1))
                    break
            
            # 如果提取到数量且与当前数量不一致，需要重新生成
            if saved_count is not None and saved_count != current_submission_count:
                should_regenerate_prompt = True
                logger.info(f"任务 {task_id} 的数据条数已更新：{saved_count} -> {current_submission_count}，重新生成提示词")
        else:
            # 如果没有保存的提示词，需要生成
            should_regenerate_prompt = True
        
        # 根据检查结果决定使用保存的提示词还是重新生成
        # 使用用户模板（如果有）生成预览提示词
        user_template = task.user_prompt_template if task.user_prompt_template else None
        interface_desc = (task.description or '').strip()  # 预览时默认用任务描述
        if should_regenerate_prompt:
            preview_prompt = generate_analysis_prompt(task, submission, file_content, SessionLocal, Submission, user_template=user_template, interface_desc=interface_desc)
            # 更新保存的提示词（但不立即提交，让用户可以选择是否保存）
        else:
            # 如果数据条数没有变化，但用户模板可能已更新，使用用户模板重新生成
            if user_template:
                preview_prompt = generate_analysis_prompt(task, submission, file_content, SessionLocal, Submission, user_template=user_template, interface_desc=interface_desc)
            else:
                preview_prompt = task.custom_prompt
        
        report = task.analysis_report if task and task.analysis_report else None
        user_prompt_template = task.user_prompt_template if task.user_prompt_template else ''

        running_flag = request.args.get('running') == '1'
        should_redirect = False
        if running_flag:
            with progress_lock:
                prog = analysis_progress.get(task.id)
            if prog and prog.get('status') == 'completed':
                should_redirect = True
        if should_redirect:
            return redirect(url_for('quickform.smart_analyze', task_id=task.id))
        
        return render_template(
            'smart_analyze.html',
            task=task,
            report=report,
            preview_prompt=preview_prompt,
            user_prompt_template=user_prompt_template,
            ai_config=ai_config,
            now=datetime.now(),
            model_label=model_label,
            submission_count=current_submission_count,
            is_large_dataset=current_submission_count > 200,
            ai_ready=ai_ready,
            ai_ready_reason=ai_ready_reason,
            all_url=all_url,
            sample_data_raw=sample_data_raw,
            dashboard_saved_name=getattr(task, 'dashboard_saved_name', None) or stable_dash_saved,
            dashboard_status=getattr(task, 'dashboard_generation_status', None),
            dashboard_error=getattr(task, 'dashboard_generation_error', None),
            dashboard_remaining=getattr(task, 'dashboard_ai_edit_remaining', None) if getattr(task, 'dashboard_ai_edit_remaining', None) is not None else 3,
            dash_running=(request.args.get('dash_running') == '1'),
        )
    finally:
        db.close()

@quickform_bp.route('/download_report/<int:task_id>')
@login_required
def download_report(task_id):
    """下载报告 - 支持 PNG（长报告分多张）、HTML、PDF"""
    fmt = (request.args.get('format') or 'png').strip().lower()
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        # 权限检查：管理员、任务所有者、被共享者、组织成员可以下载报告
        has_access = False
        if current_user.is_admin() or task.user_id == current_user.id:
            has_access = True
        elif db.query(TaskShare).filter_by(
            task_id=task.id,
            user_id=current_user.id
        ).first():
            has_access = True
        elif task.organization_id:
            is_org_member = db.query(OrganizationMember).filter_by(
                organization_id=task.organization_id,
                user_id=current_user.id
            ).first() is not None
            if is_org_member:
                has_access = True
        
        if not has_access:
            flash('无权访问此任务', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        report_content = task.analysis_report or "暂无报告内容"
        safe_title = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', task.title)[:50]
        
        if fmt == 'html':
            # HTML 导出：始终从当前报告内容动态生成，确保 Markdown 被渲染为 HTML（不依赖旧缓存文件）
            html_str = build_report_html(task, report_content)
            response = make_response(html_str)
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
            response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{url_quote((safe_title + '_report.html').encode('utf-8'))}"
            return response
        
        if fmt == 'pdf':
            # PDF 导出：优先 weasyprint，在 Windows 上若出现 gobject/GTK 错误则回退到 xhtml2pdf；使用中文字体以正确显示中文
            html_str = build_report_html(task, report_content, for_pdf=True)
            pdf_io = io.BytesIO()
            pdf_ok = False
            try:
                from weasyprint import HTML as WeasyHTML
                WeasyHTML(string=html_str).write_pdf(pdf_io)
                pdf_ok = True
            except (ImportError, OSError, Exception) as e:
                logger.warning(f"weasyprint 不可用（{e}），尝试 xhtml2pdf")
                try:
                    from xhtml2pdf import pisa
                    pdf_io = io.BytesIO()
                    pisa_status = pisa.CreatePDF(html_str, dest=pdf_io, encoding='utf-8')
                    if not pisa_status.err:
                        pdf_ok = True
                except ImportError:
                    flash('PDF 导出需要安装 weasyprint 或 xhtml2pdf。Windows 推荐: pip install xhtml2pdf', 'warning')
                    return redirect(url_for('quickform.smart_analyze', task_id=task_id))
                except Exception as e2:
                    logger.error(f"xhtml2pdf 生成 PDF 失败: {str(e2)}", exc_info=True)
                    flash(f'生成 PDF 时出错: {str(e2)}', 'danger')
                    return redirect(url_for('quickform.smart_analyze', task_id=task_id))
            if pdf_ok:
                pdf_io.seek(0)
                response = make_response(pdf_io.getvalue())
                response.headers['Content-Type'] = 'application/pdf'
                response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{url_quote((safe_title + '_report.pdf').encode('utf-8'))}"
                return response
            flash('PDF 生成失败', 'danger')
            return redirect(url_for('quickform.smart_analyze', task_id=task_id))
        
        # 默认：PNG 图片（长报告分多张，打包为 zip）
        buffers, filenames = generate_report_image(task, report_content)
        if len(buffers) == 1:
            response = make_response(buffers[0].getvalue())
            response.headers['Content-Type'] = 'image/png'
            response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{url_quote(filenames[0].encode('utf-8'))}"
            return response
        zip_io = io.BytesIO()
        with zipfile.ZipFile(zip_io, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, (buf, name) in enumerate(zip(buffers, filenames)):
                zf.writestr(name, buf.getvalue())
        zip_io.seek(0)
        response = make_response(zip_io.getvalue())
        response.headers['Content-Type'] = 'application/zip'
        response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{url_quote((safe_title + '_report_图片.zip').encode('utf-8'))}"
        return response
        
    except Exception as e:
        logger.exception("下载报告失败: %s", e)
        flash('下载报告失败，请稍后重试。', 'danger')
        return redirect(url_for('quickform.dashboard'))
    finally:
        db.close()


@quickform_bp.route('/analyze/<int:task_id>/dashboard_status', methods=['GET'])
@login_required
def dashboard_status(task_id):
    """数据大屏生成状态（smart_analyze 页面轮询使用）"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            return jsonify({'success': False, 'message': '任务不存在'}), 404
        # 权限：与 smart_analyze 相同（管理员/所有者/共享/组织成员）
        has_access = False
        if current_user.is_admin() or task.user_id == current_user.id:
            has_access = True
        elif db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first():
            has_access = True
        elif task.organization_id:
            is_org_member = db.query(OrganizationMember).filter_by(
                organization_id=task.organization_id, user_id=current_user.id
            ).first() is not None
            if is_org_member:
                has_access = True
        if not has_access:
            return jsonify({'success': False, 'message': '无权访问此任务'}), 403

        api_base_url = _public_site_base_url()
        saved_name = getattr(task, 'dashboard_saved_name', None)
        dash_url = None
        if saved_name:
            # static/uploads/<saved_name>
            dash_url = f"{api_base_url}/static/uploads/{saved_name}"
        return jsonify({
            'success': True,
            'status': getattr(task, 'dashboard_generation_status', None),
            'error': getattr(task, 'dashboard_generation_error', None),
            'remaining': getattr(task, 'dashboard_ai_edit_remaining', None),
            'dash_url': dash_url,
            'generated_at': task.dashboard_generated_at.strftime('%Y-%m-%d %H:%M:%S') if getattr(task, 'dashboard_generated_at', None) else None
        })
    finally:
        db.close()

@quickform_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """上传文件访问 - 原有 uploads 目录中的文件由此路由提供；已在 static/uploads 的新文件重定向到静态 URL"""
    try:
        legacy_path = os.path.join(UPLOAD_FOLDER, filename)
        static_path = os.path.join(_static_uploads_dir(), filename)
        if not os.path.exists(legacy_path) and os.path.exists(static_path):
            return redirect(url_for('static', filename='uploads/' + filename))
        # 检查文件扩展名，如果是HTML文件需要检查审核状态
        if filename.lower().endswith(('.html', '.htm')):
            db = SessionLocal()
            try:
                # 查找包含此文件名的任务
                task = db.query(Task).filter(Task.file_path.like(f'%{filename}')).first()
                if task:
                    # 管理员可直接访问原始文件
                    if current_user.is_authenticated and current_user.is_admin():
                        return send_from_directory(UPLOAD_FOLDER, filename)
                    # 检查审核状态
                    if task.html_approved != 1:
                        if task.html_approved == -1:
                            reason = html.escape(task.html_review_note or '管理员未提供原因')
                            title_text = '审核未通过'
                            message = f"页面未通过审核，原因：{reason}"
                            status_icon = '❌'
                        else:
                            title_text = '审核中'
                            message = '该页面正在等待管理员审核，审核通过后即可访问。'
                            status_icon = '⏳'

                        html_content = f"""
<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
    <meta charset=\"UTF-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
    <title>{title_text}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            background-color: #f5f5f5;
            padding: 16px;
        }}
        .container {{
            max-width: 520px;
            text-align: center;
            padding: 40px;
            background: white;
            border-radius: 12px;
            box-shadow: 0 12px 32px rgba(15, 23, 42, 0.1);
        }}
        h1 {{ color: #333; margin-bottom: 16px; font-size: 24px; }}
        p {{ color: #555; margin-top: 12px; line-height: 1.6; }}
    </style>
</head>
<body>
    <div class=\"container\">
        <h1>{status_icon} {title_text}</h1>
        <p>{message}</p>
    </div>
</body>
</html>
                        """
                        response = make_response(html_content)
                        response.headers['Content-Type'] = 'text/html; charset=utf-8'
                        return response
                # 如果找不到任务或已审核通过，允许访问
            finally:
                db.close()
            
            # 读取HTML文件内容并注入增强脚本
            try:
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                if os.path.exists(filepath):
                    with open(filepath, 'r', encoding='utf-8') as f:
                        html_content = f.read()
                    
                    # 构建增强脚本的URL
                    # 使用请求的基础URL构建静态文件路径
                    base_url = request.url_root.rstrip('/')
                    script_url = f'{base_url}/static/js/form-enhancements.js'
                    
                    # 注入增强脚本
                    enhancement_script = f'<script src="{script_url}"></script>'
                    
                    # 在</head>之前注入，如果没有</head>则在</body>之前注入
                    if '</head>' in html_content:
                        html_content = html_content.replace('</head>', f'{enhancement_script}\n</head>', 1)
                    elif '</body>' in html_content:
                        html_content = html_content.replace('</body>', f'{enhancement_script}\n</body>', 1)
                    else:
                        # 如果没有找到，在文件末尾添加
                        html_content += f'\n{enhancement_script}\n'
                    
                    response = make_response(html_content)
                    response.headers['Content-Type'] = 'text/html; charset=utf-8'
                    return response
            except Exception as e:
                logger.warning(f"注入增强脚本失败，返回原始文件: {str(e)}")
            
            return send_from_directory(UPLOAD_FOLDER, filename)
        else:
            # TXT 文件开放访问，便于公网直接查看
            if filename.lower().endswith('.txt'):
                return send_from_directory(UPLOAD_FOLDER, filename)
            # 其他非 HTML 文件仍需登录保护
            if not current_user.is_authenticated:
                flash('请先登录', 'warning')
                return redirect(url_for('quickform.login'))
            return send_from_directory(UPLOAD_FOLDER, filename)
    except FileNotFoundError:
        flash('文件不存在', 'danger')
        return redirect(request.referrer or url_for('quickform.dashboard'))

@quickform_bp.route('/generate_report/<int:task_id>', methods=['GET', 'POST'])
@login_required
def generate_report(task_id):
    """兼容旧链接：重定向到智能分析页面"""
    return redirect(url_for('quickform.smart_analyze', task_id=task_id))

@quickform_bp.route('/api/report_status/<int:task_id>', methods=['GET'])
@login_required
def report_status(task_id):
    """查询报告生成进度/结果（供前端轮询）"""
    try:
            # 权限检查：管理员、任务所有者、被共享者、组织成员可以查看报告状态
        db = SessionLocal()
        try:
            task = db.get(Task, task_id)
            if not task:
                return jsonify({'status': 'error', 'message': '任务不存在'}), 404
            
            has_access = False
            if current_user.is_admin() or task.user_id == current_user.id:
                has_access = True
            elif db.query(TaskShare).filter_by(
                task_id=task.id,
                user_id=current_user.id
            ).first():
                has_access = True
            elif task.organization_id:
                is_org_member = db.query(OrganizationMember).filter_by(
                    organization_id=task.organization_id,
                    user_id=current_user.id
                ).first() is not None
                if is_org_member:
                    has_access = True
            
            if not has_access:
                return jsonify({'status': 'error', 'message': '无权访问此任务'}), 403
        finally:
            db.close()
        
        with progress_lock:
            prog = analysis_progress.get(task_id)
            if prog:
                # 如果已完成且内存中有报告，直接返回报告
                if prog.get('status') == 'completed':
                    rep = analysis_results.get(task_id)
                    return jsonify({'status': 'completed', 'report': rep or prog.get('report', '')}), 200
                if prog.get('status') == 'error':
                    return jsonify({'status': 'error', 'message': prog.get('message', '未知错误')}), 200
                # 进行中
                return jsonify({'status': 'in_progress', 'progress': prog.get('progress', 0), 'message': prog.get('message', '')}), 200
        # 兜底：查数据库是否已有报告
        db = SessionLocal()
        try:
            task = db.get(Task, task_id)
            if task and task.analysis_report:
                return jsonify({'status': 'completed', 'report': task.analysis_report}), 200
        finally:
            db.close()
        return jsonify({'status': 'not_started'}), 200
    except Exception as e:
        logger.exception("report_status 异常: %s", e)
        return jsonify({'status': 'error', 'message': MSG_API_INTERNAL}), 500

@quickform_bp.route('/admin')
@admin_required
def admin_panel():
    """管理员面板：按当前 tab 仅加载该页数据，避免一次性查全表"""
    from urllib.parse import urlencode

    current_tab = request.args.get('tab', 'users')
    if current_tab == 'traffic':
        current_tab = 'tasks'
    # 旧链接 tab=public-review|org-review|html-review 统一到「其他审核」主视图，避免子 tab 切换跳页
    _other_review_frag = {
        'public-review': 'section-public-audit',
        'org-review': 'section-org-audit',
        'html-review': 'section-html-audit',
    }
    if current_tab in _other_review_frag:
        q = request.args.to_dict()
        q['tab'] = 'other-review'
        frag = _other_review_frag[current_tab]
        return redirect(f"{request.path}?{urlencode(q)}#{frag}")

    db = SessionLocal()
    try:
        today = datetime.now().date()
        today_start = datetime.combine(today, datetime.min.time())
        user_per_page = 20
        task_per_page = 20
        html_review_per_page = 20
        cert_review_per_page = 20

        # 顶部统计：用户/管理员/任务始终 count；提交总数不在后台首页自动查询（避免全表扫 submission）
        total_users = db.query(User).count()
        admin_users = db.query(User).filter_by(role='admin').count()
        certified_users_top = db.query(User).filter(User.is_certified == True).count()
        total_tasks = db.query(Task).count()
        total_submissions = None
        pending_cert_sidebar = db.query(CertificationRequest).filter(CertificationRequest.status == 0).count()
        pending_html_sidebar = db.query(Task).filter(Task.html_approved != 1).count()
        pending_public_sidebar = db.query(Task).filter(Task.sharing_type == 'public', Task.public_approved == 0).count()
        pending_org_sidebar = db.query(Organization).filter(
            Organization.teams_public_requested == True,
            Organization.teams_public_approved == 0,
        ).count()
        # 「其他审核」侧栏数字：仅公开项目 + 团队入驻待审，不含 HTML 页面审核（HTML 在子 tab 内单独展示）
        pending_other_sidebar = pending_public_sidebar + pending_org_sidebar
        pending_quota_sidebar = db.query(TaskQuotaRequest).filter(TaskQuotaRequest.status == 0).count()

        # 默认值：未选中的 tab 不查数据
        users = []
        total_filtered_users = 0
        user_total_pages = 1
        user_page = 1
        search_keyword = (request.args.get('q') or '').strip()

        all_tasks = []
        task_total_pages = 1
        task_page = 1

        html_tasks_with_review = []
        pending_html_count = 0
        html_review_total_pages = 1
        html_review_page = 1
        total_html_tasks = 0

        cert_requests = []
        pending_cert_count = 0
        cert_review_total_pages = 1
        cert_review_page = 1
        total_cert_requests = 0

        public_pending_with_author = []
        public_pending_html_urls = []
        org_pending_with_creator = []
        open_source_tasks_with_author = []
        tutorials_json_content = '[]'
        quota_requests_pending = []
        quota_requests_recent = []
        site_quota_default_row = None
        oneclick_prompt_rows = []
        submit_quota_base_c = None
        submit_quota_base_b = None

        # ---------- 仅当前 tab 才执行对应查询 ----------
        if current_tab == 'users':
            user_page = request.args.get('user_page', 1, type=int) or 1
            user_page = max(1, user_page)
            user_query = db.query(User)
            if search_keyword:
                like_pattern = f"%{search_keyword}%"
                user_query = user_query.filter(
                    or_(
                        User.username.ilike(like_pattern),
                        User.email.ilike(like_pattern),
                        User.school.ilike(like_pattern),
                        User.phone.ilike(like_pattern)
                    )
                )
            total_filtered_users = user_query.count()
            user_total_pages = max(math.ceil(total_filtered_users / user_per_page), 1) if total_filtered_users else 1
            user_page = min(user_page, user_total_pages)
            users = (
                user_query
                .order_by(User.created_at.desc())
                .offset((user_page - 1) * user_per_page)
                .limit(user_per_page)
                .all()
            )

        elif current_tab == 'tasks':
            task_page = request.args.get('task_page', 1, type=int) or 1
            task_page = max(1, task_page)
            task_total_pages = max(math.ceil(total_tasks / task_per_page), 1) if total_tasks else 1
            task_page = min(task_page, task_total_pages)
            submit_quota_base_c, submit_quota_base_b = _get_site_submit_quota_defaults(db)
            all_tasks = (
                db.query(Task)
                .options(joinedload(Task.author))
                .order_by(Task.created_at.desc())
                .offset((task_page - 1) * task_per_page)
                .limit(task_per_page)
                .all()
            )

        elif current_tab == 'other-review':
            # other-review 页面会同时展示：公开项目审核 / 团队入驻审核 / HTML 页面审核
            # 因此需要一次性加载三类数据；避免 elif 链路提前命中导致其它审核列表为空
            html_review_page = request.args.get('html_review_page', 1, type=int) or 1
            html_review_page = max(1, html_review_page)
            html_tasks_query = db.query(Task).filter(
                Task.file_path.isnot(None),
                Task.file_name.isnot(None),
                (Task.file_name.like('%.html') | Task.file_name.like('%.htm'))
            )
            total_html_tasks = html_tasks_query.count()
            html_review_total_pages = max(math.ceil(total_html_tasks / html_review_per_page), 1) if total_html_tasks else 1
            html_review_page = min(html_review_page, html_review_total_pages)
            html_tasks = (
                html_tasks_query
                .order_by(Task.created_at.desc())
                .offset((html_review_page - 1) * html_review_per_page)
                .limit(html_review_per_page)
                .all()
            )
            user_ids = {t.user_id for t in html_tasks} | {t.html_approved_by for t in html_tasks if t.html_approved_by}
            user_ids.discard(None)
            authors_map = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
            for task in html_tasks:
                html_tasks_with_review.append({
                    'task': task,
                    'author': authors_map.get(task.user_id),
                    'approver': authors_map.get(task.html_approved_by) if task.html_approved_by else None
                })
                if task.html_approved != 1:
                    pending_html_count += 1

            public_pending_tasks = (
                db.query(Task)
                .filter(Task.sharing_type == 'public', Task.public_approved == 0)
                .order_by(Task.created_at.desc())
                .all()
            )
            author_ids = {t.user_id for t in public_pending_tasks}
            authors_map2 = {u.id: u for u in db.query(User).filter(User.id.in_(author_ids)).all()} if author_ids else {}
            public_pending_with_author = [{'task': t, 'author': authors_map2.get(t.user_id)} for t in public_pending_tasks]
            _seen_pub_html_urls = set()
            for row in public_pending_with_author:
                row['html_links'] = _task_html_file_links(row['task'])
                for hl in row['html_links']:
                    u = hl['url']
                    if u not in _seen_pub_html_urls:
                        _seen_pub_html_urls.add(u)
                        public_pending_html_urls.append(u)

            org_pending = (
                db.query(Organization)
                .filter(
                    Organization.teams_public_requested == True,
                    Organization.teams_public_approved == 0,
                )
                .order_by(Organization.created_at.desc())
                .all()
            )
            creator_ids = {o.creator_id for o in org_pending}
            creators_map = {u.id: u for u in db.query(User).filter(User.id.in_(creator_ids)).all()} if creator_ids else {}
            org_pending_with_creator = [{'org': o, 'creator': creators_map.get(o.creator_id)} for o in org_pending]

        elif current_tab == 'cert-review':
            cert_review_page = request.args.get('cert_review_page', 1, type=int) or 1
            cert_review_page = max(1, cert_review_page)
            total_cert_requests = db.query(CertificationRequest).count()
            cert_review_total_pages = max(math.ceil(total_cert_requests / cert_review_per_page), 1) if total_cert_requests else 1
            cert_review_page = min(cert_review_page, cert_review_total_pages)
            cert_requests = (
                db.query(CertificationRequest)
                .order_by(CertificationRequest.created_at.desc())
                .offset((cert_review_page - 1) * cert_review_per_page)
                .limit(cert_review_per_page)
                .all()
            )
            pending_cert_count = (
                db.query(CertificationRequest)
                .filter(CertificationRequest.status == 0)
                .count()
            )

        elif current_tab == 'data':
            pass  # stats 在下方按 tab 计算

        elif current_tab == 'open-source':
            open_tasks = (
                db.query(Task)
                .filter(Task.sharing_type == 'public', Task.public_approved == 1)
                .order_by(Task.created_at.desc())
                .all()
            )
            author_ids = {t.user_id for t in open_tasks}
            authors_map = {u.id: u for u in db.query(User).filter(User.id.in_(author_ids)).all()} if author_ids else {}
            open_source_tasks_with_author = [{'task': t, 'author': authors_map.get(t.user_id)} for t in open_tasks]

        elif current_tab == 'tutorials-edit':
            try:
                tutorials_dir = os.path.join(current_app.static_folder, 'tutorials')
                json_path = os.path.join(tutorials_dir, 'tutorials.json')
                if os.path.exists(json_path):
                    with open(json_path, 'r', encoding='utf-8') as f:
                        tutorials_json_content = f.read()
            except Exception as e:
                logger.warning(f"读取 tutorials.json 失败: {e}")

        elif current_tab == 'quota-review':
            quota_requests_pending = (
                db.query(TaskQuotaRequest)
                .options(joinedload(TaskQuotaRequest.task), joinedload(TaskQuotaRequest.applicant))
                .filter(TaskQuotaRequest.status == 0)
                .order_by(TaskQuotaRequest.created_at.desc())
                .all()
            )
            quota_requests_recent = (
                db.query(TaskQuotaRequest)
                .options(
                    joinedload(TaskQuotaRequest.task),
                    joinedload(TaskQuotaRequest.applicant),
                    joinedload(TaskQuotaRequest.reviewer),
                )
                .filter(TaskQuotaRequest.status != 0)
                .order_by(TaskQuotaRequest.id.desc())
                .limit(40)
                .all()
            )

        elif current_tab == 'quota-settings':
            site_quota_default_row = db.get(SiteQuotaDefault, 1)

        elif current_tab == 'oneclick-prompts':
            oneclick_prompt_rows = (
                db.query(OneclickPromptOption)
                .order_by(OneclickPromptOption.sort_order.asc(), OneclickPromptOption.id.asc())
                .all()
            )

        # 流量预估：仅当前 tab 为 traffic 时使用
        api_traffic = []
        if current_tab == 'traffic':
            with _api_counts_lock:
                copy_counts = dict(_api_get_counts)
            labels = {
                'api_task_get': 'GET /api/<task_id>（最新3条）',
                'api_task_all': 'GET /api/<task_id>/all（全部数据，数据大屏）',
                'api_tasks': 'GET /api/tasks（任务列表）',
                'api_stats_overview': 'GET /api/stats/overview（运营统计）',
            }
            for key in ['api_task_get', 'api_task_all', 'api_tasks', 'api_stats_overview']:
                api_traffic.append({
                    'category': labels.get(key, key),
                    'count': copy_counts.get(key, 0),
                })
            api_traffic.sort(key=lambda x: -x['count'])

        # 统计：仅顶部 4 项始终有；进入「数据报表」tab 再算完整 stats
        stats = {
            'total_users': total_users,
            'admin_users': admin_users,
            'total_tasks': total_tasks,
            'total_submissions': total_submissions,
            'certified_users': certified_users_top,
            'online_now': (lambda: (lambda x: x)(0))(),  # placeholder, overwritten below
            'normal_users': 0,
            'new_users_today': 0,
            'new_tasks_today': 0,
            'avg_tasks_per_user': 0,
            'new_submissions_today': 0,
            'avg_submissions_per_task': 0,
            'tasks_with_reports': 0,
            'report_generation_rate': 0,
            'total_organizations': 0,
            'total_org_members': 0,
            'tasks_in_organizations': 0,
            'public_tasks': 0,
            'public_approved_tasks': 0,
            'total_task_shares': 0,
            'total_task_likes': 0,
            'ai_generated_tasks': 0,
            'cert_requests_pending': 0,
            'total_posts': 0,
            'total_post_replies': 0,
        }
        if current_tab == 'data':
            # 默认进入「数据报表」仅展示每日注册人数（避免一次性扫全表统计）
            try:
                stats['new_users_today'] = db.query(User).filter(User.created_at >= today_start).count()
            except Exception:
                stats['new_users_today'] = 0

        # 实时在线：当前正在处理的网页请求数（近似老师端在线）
        try:
            with _ONLINE_LOCK:
                stats['online_now'] = int(_ONLINE_INFLIGHT_WEB)
        except Exception:
            stats['online_now'] = 0

        return render_template(
            'admin.html',
            users=users,
            all_tasks=all_tasks,
            stats=stats,
            user_search=search_keyword,
            user_page=user_page,
            user_pages=user_total_pages,
            user_total=total_filtered_users,
            user_per_page=user_per_page,
            task_page=task_page,
            task_pages=task_total_pages,
            task_total=total_tasks,
            task_per_page=task_per_page,
            html_tasks_with_review=html_tasks_with_review,
            pending_html_count=pending_html_count,
            html_review_page=html_review_page,
            html_review_pages=html_review_total_pages,
            html_review_total=total_html_tasks,
            html_review_per_page=html_review_per_page,
            cert_requests=cert_requests,
            pending_cert_count=pending_cert_count,
            cert_review_page=cert_review_page,
            cert_review_pages=cert_review_total_pages,
            cert_review_total=total_cert_requests,
            cert_review_per_page=cert_review_per_page,
            current_tab=current_tab,
            public_pending_with_author=public_pending_with_author,
            public_pending_html_urls=public_pending_html_urls,
            org_pending_with_creator=org_pending_with_creator,
            open_source_tasks_with_author=open_source_tasks_with_author,
            tutorials_json_content=tutorials_json_content,
            api_traffic=api_traffic,
            quota_requests_pending=quota_requests_pending,
            quota_requests_recent=quota_requests_recent,
            site_quota_default_row=site_quota_default_row,
            oneclick_prompt_rows=oneclick_prompt_rows,
            submit_quota_base_c=submit_quota_base_c,
            submit_quota_base_b=submit_quota_base_b,
            pending_cert_sidebar=pending_cert_sidebar,
            pending_other_sidebar=pending_other_sidebar,
            pending_quota_sidebar=pending_quota_sidebar,
        )
    finally:
        db.close()


@quickform_bp.route('/admin/oneclick_prompt_options/save', methods=['POST'])
@admin_required
def admin_save_oneclick_prompt_options():
    """保存一键生成任务时勾选追加的说明文案（管理员）。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(OneclickPromptOption)
            .order_by(OneclickPromptOption.sort_order.asc(), OneclickPromptOption.id.asc())
            .all()
        )
        if not rows:
            flash('数据库中尚无一键生成选项记录，请重启应用以完成数据库迁移后再试。', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='oneclick-prompts'))
        for row in rows:
            lab = (request.form.get(f'label_{row.opt_key}') or '').strip()
            bod = (request.form.get(f'body_{row.opt_key}') or '').strip()
            if not lab:
                flash(f'选项「{row.opt_key}」的显示名称不能为空', 'warning')
                return redirect(url_for('quickform.admin_panel', tab='oneclick-prompts'))
            row.label = lab[:200]
            row.body = bod
            row.updated_at = datetime.now()
        db.commit()
        flash('已保存一键生成「追加到需求后的说明」文案。', 'success')
    except Exception as e:
        db.rollback()
        logger.exception('admin_save_oneclick_prompt_options: %s', e)
        flash('保存失败', 'danger')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='oneclick-prompts'))


@quickform_bp.route('/admin/site_quota_defaults', methods=['POST'])
@admin_required
def admin_save_site_quota_defaults():
    """保存全站默认限额：/all 读取 + 提交写入（每任务基础值，不含单任务加额）"""
    raw_r = (request.form.get('default_all_read_limit') or '').strip()
    raw_mb = (request.form.get('default_all_bytes_mb') or '').strip()
    raw_submit_c = (request.form.get('default_submit_count_limit') or '').strip()
    raw_submit_mb = (request.form.get('default_submit_bytes_mb') or '').strip()
    auto_enabled_raw = (request.form.get('auto_quota_approve_enabled') or '').strip()
    auto_reads_raw = (request.form.get('auto_quota_approve_max_reads') or '0').strip()
    auto_mb_raw = (request.form.get('auto_quota_approve_max_mb') or '0').strip()
    try:
        r = int(raw_r)
        mb = int(raw_mb)
        submit_c = int(raw_submit_c)
        submit_mb = int(raw_submit_mb)
        auto_reads = max(0, int(auto_reads_raw or 0))
        auto_mb = max(0, int(auto_mb_raw or 0))
    except ValueError:
        flash('请填写有效的整数', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-settings'))
    auto_enabled = 1 if auto_enabled_raw in ('1', 'true', 'on', 'yes') else 0
    if r < 1 or r > 50_000_000:
        flash('「每任务 /all 次数」须在 1～50000000 之间', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-settings'))
    if mb < 1 or mb > 1_048_576:
        flash('「每任务流量」以 MB 为单位，须在 1～1048576（约 1TB）之间', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-settings'))
    # submit_c=0 表示不限次数
    if submit_c < 0 or submit_c > 50_000_000:
        flash('「每任务提交条数」须在 0～50000000 之间（0 表示不限）', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-settings'))
    if submit_mb < 1 or submit_mb > 1_048_576:
        flash('「每任务提交累计字节」以 MB 为单位，须在 1～1048576（约 1TB）之间', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-settings'))
    db = SessionLocal()
    try:
        row = db.get(SiteQuotaDefault, 1)
        if not row:
            row = SiteQuotaDefault(
                id=1,
                default_all_read_limit=r,
                default_all_bytes_limit=mb * 1024 * 1024,
                default_submit_count_limit=submit_c,
                default_submit_bytes_limit=submit_mb * 1024 * 1024,
                auto_quota_approve_enabled=auto_enabled,
                auto_quota_approve_max_reads=auto_reads,
                auto_quota_approve_max_mb=auto_mb,
            )
            db.add(row)
        else:
            row.default_all_read_limit = r
            row.default_all_bytes_limit = mb * 1024 * 1024
            row.default_submit_count_limit = submit_c
            row.default_submit_bytes_limit = submit_mb * 1024 * 1024
            row.auto_quota_approve_enabled = auto_enabled
            row.auto_quota_approve_max_reads = auto_reads
            row.auto_quota_approve_max_mb = auto_mb
            row.updated_at = datetime.now()
        db.commit()
        flash('已保存全站默认限额（/all 读取 + 提交写入），立即生效。', 'success')
    except Exception as e:
        db.rollback()
        logger.exception('admin_save_site_quota_defaults: %s', e)
        flash('保存失败', 'danger')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='quota-settings'))


@quickform_bp.route('/admin/task_submit_quota/<int:task_id>', methods=['POST'])
@admin_required
def admin_save_task_submit_quota(task_id):
    """管理员调整单任务的「提交写入」加额（在全局默认基础上累加）。"""
    extra_c_raw = (request.form.get('quota_extra_submit_count') or '0').strip()
    extra_mb_raw = (request.form.get('quota_extra_submit_mb') or '0').strip()
    try:
        extra_c = max(0, int(extra_c_raw or 0))
        extra_mb = max(0, int(extra_mb_raw or 0))
    except ValueError:
        flash('提交加额请输入非负整数', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='tasks'))
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='tasks'))
        task.quota_extra_submit_count = extra_c
        task.quota_extra_submit_bytes = int(extra_mb) * 1024 * 1024
        db.commit()
        bc, bb = _get_site_submit_quota_defaults(db)
        lim_c = int(bc) + int(task.quota_extra_submit_count or 0)
        lim_b = int(bb) + int(task.quota_extra_submit_bytes or 0)
        flash(
            f'已保存任务 {task_id} 的提交加额：额外 {extra_c} 条、额外 {extra_mb} MB（有效上限约 {lim_c} 条 / {lim_b // (1024 * 1024)} MB）。',
            'success',
        )
    except Exception as e:
        db.rollback()
        logger.exception('admin_save_task_submit_quota: %s', e)
        flash('保存失败', 'danger')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='tasks'))


@quickform_bp.route('/admin/task_quota_approve/<int:req_id>', methods=['POST'])
@admin_required
def admin_task_quota_approve(req_id):
    """管理员通过任务的 /all 加额申请"""
    db = SessionLocal()
    try:
        req = db.get(TaskQuotaRequest, req_id)
        if not req or req.status != 0:
            flash('申请不存在或已处理', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='quota-review'))
        default_reads = int(getattr(req, 'requested_extra_reads', 0) or 0)
        default_mb = int(getattr(req, 'requested_extra_mb', 0) or 0)
        extra_reads_raw = (request.form.get('granted_extra_reads') or str(default_reads)).strip()
        extra_mb_raw = (request.form.get('granted_extra_mb') or str(default_mb)).strip()
        try:
            er = max(0, int(extra_reads_raw or 0))
            emb = max(0, int(extra_mb_raw or 0))
        except ValueError:
            flash('加额请输入非负整数', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='quota-review'))
        if er == 0 and emb == 0:
            flash('请至少填写一项加额（/all 次数或 MB）', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='quota-review'))
        task = db.get(Task, req.task_id)
        if not task:
            flash('任务不存在', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='quota-review'))
        task.quota_extra_all_reads = (task.quota_extra_all_reads or 0) + er
        task.quota_extra_all_bytes = int(task.quota_extra_all_bytes or 0) + emb * 1024 * 1024
        req.status = 1
        req.reviewed_at = datetime.now()
        req.reviewed_by = current_user.id
        req.granted_extra_reads = er
        req.granted_extra_mb = emb
        rn = (request.form.get('review_note') or '').strip()
        req.review_note = rn or None
        br, bb = _get_site_all_quota_defaults(db)
        db.commit()
        flash(
            f'已通过加额：+{er} 次 /all、+{emb} MB（在全局默认 {br} 次 / {bb // (1024 * 1024)} MB 之上累加）',
            'success',
        )
    except Exception as e:
        db.rollback()
        logger.exception('admin_task_quota_approve: %s', e)
        flash('处理失败', 'danger')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='quota-review'))


@quickform_bp.route('/admin/task_quota_reject/<int:req_id>', methods=['POST'])
@admin_required
def admin_task_quota_reject(req_id):
    """管理员拒绝任务的 /all 加额申请"""
    db = SessionLocal()
    try:
        req = db.get(TaskQuotaRequest, req_id)
        if not req or req.status != 0:
            flash('申请不存在或已处理', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='quota-review'))
        req.status = -1
        req.reviewed_at = datetime.now()
        req.reviewed_by = current_user.id
        rn = (request.form.get('review_note') or '').strip()
        req.review_note = rn or None
        db.commit()
        flash('已拒绝该加额申请', 'info')
    except Exception as e:
        db.rollback()
        logger.exception('admin_task_quota_reject: %s', e)
        flash('处理失败', 'danger')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='quota-review'))


@quickform_bp.route('/admin/task_quota_batch_approve', methods=['POST'])
@admin_required
def admin_task_quota_batch_approve():
    """批量通过加额申请（所选记录使用相同的 +次数 / +MB / 备注）"""
    ids = request.form.getlist('request_ids')
    extra_reads_raw = (request.form.get('granted_extra_reads') or '0').strip()
    extra_mb_raw = (request.form.get('granted_extra_mb') or '0').strip()
    try:
        er = max(0, int(extra_reads_raw or 0))
        emb = max(0, int(extra_mb_raw or 0))
    except ValueError:
        flash('加额请输入非负整数', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-review'))
    if er == 0 and emb == 0:
        flash('请至少填写一项加额（/all 次数或 MB）', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-review'))
    if not ids:
        flash('请先勾选要通过的申请', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-review'))
    rn = (request.form.get('review_note') or '').strip() or None
    db = SessionLocal()
    ok = 0
    try:
        for sid in ids:
            try:
                rid = int(sid)
            except (ValueError, TypeError):
                continue
            req = db.get(TaskQuotaRequest, rid)
            if not req or req.status != 0:
                continue
            task = db.get(Task, req.task_id)
            if not task:
                continue
            task.quota_extra_all_reads = (task.quota_extra_all_reads or 0) + er
            task.quota_extra_all_bytes = int(task.quota_extra_all_bytes or 0) + emb * 1024 * 1024
            req.status = 1
            req.reviewed_at = datetime.now()
            req.reviewed_by = current_user.id
            req.granted_extra_reads = er
            req.granted_extra_mb = emb
            req.review_note = rn
            ok += 1
        db.commit()
        flash(f'已批量通过 {ok} 条申请（各 +{er} 次 /all、+{emb} MB）', 'success')
    except Exception as e:
        db.rollback()
        logger.exception('admin_task_quota_batch_approve: %s', e)
        flash('批量处理失败', 'danger')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='quota-review'))


@quickform_bp.route('/admin/task_quota_batch_reject', methods=['POST'])
@admin_required
def admin_task_quota_batch_reject():
    """批量拒绝加额申请"""
    ids = request.form.getlist('request_ids')
    if not ids:
        flash('请先勾选要拒绝的申请', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='quota-review'))
    rn = (request.form.get('review_note') or '').strip() or None
    db = SessionLocal()
    ok = 0
    try:
        for sid in ids:
            try:
                rid = int(sid)
            except (ValueError, TypeError):
                continue
            req = db.get(TaskQuotaRequest, rid)
            if not req or req.status != 0:
                continue
            req.status = -1
            req.reviewed_at = datetime.now()
            req.reviewed_by = current_user.id
            req.review_note = rn
            ok += 1
        db.commit()
        flash(f'已批量拒绝 {ok} 条申请', 'info')
    except Exception as e:
        db.rollback()
        logger.exception('admin_task_quota_batch_reject: %s', e)
        flash('批量拒绝失败', 'danger')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='quota-review'))


@quickform_bp.route('/admin/public_approve/<int:task_id>', methods=['POST'])
@admin_required
def admin_public_approve(task_id):
    """管理员通过项目公开申请"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.sharing_type != 'public' or task.public_approved != 0:
            flash('任务不存在或无需审核', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-public-audit')
        task.public_approved = 1
        db.commit()
        flash(f'已通过项目「{task.title}」的公开申请，将展示在项目交流页。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-public-audit')


@quickform_bp.route('/admin/public_reject/<int:task_id>', methods=['POST'])
@admin_required
def admin_public_reject(task_id):
    """管理员拒绝项目公开申请"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.sharing_type != 'public' or task.public_approved != 0:
            flash('任务不存在或无需审核', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-public-audit')
        task.public_approved = -1
        db.commit()
        flash(f'已拒绝项目「{task.title}」的公开申请。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-public-audit')


@quickform_bp.route('/admin/org_teams_approve/<int:org_id>', methods=['POST'])
@admin_required
def admin_org_teams_approve(org_id):
    """管理员通过组织「入驻团队 / 首页公开」申请"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org or not org.teams_public_requested or org.teams_public_approved != 0:
            flash('组织不存在或无需审核', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-org-audit')
        org.teams_public_approved = 1
        db.commit()
        flash(f'已通过组织「{org.name}」的入驻团队展示申请。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-org-audit')


@quickform_bp.route('/admin/org_teams_reject/<int:org_id>', methods=['POST'])
@admin_required
def admin_org_teams_reject(org_id):
    """管理员拒绝组织「入驻团队」申请"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org or not org.teams_public_requested or org.teams_public_approved != 0:
            flash('组织不存在或无需审核', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-org-audit')
        org.teams_public_approved = -1
        db.commit()
        flash(f'已拒绝组织「{org.name}」的入驻团队展示申请。创建者可改为「内部交流」后重新申请。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-org-audit')


@quickform_bp.route('/admin/public_batch_approve', methods=['POST'])
@admin_required
def admin_public_batch_approve():
    """管理员批量通过项目公开申请"""
    task_ids = request.form.getlist('task_ids')
    if not task_ids:
        flash('请先勾选要通过的项目', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-public-audit')
    db = SessionLocal()
    try:
        count = 0
        for tid in task_ids:
            try:
                task_id = int(tid)
            except (ValueError, TypeError):
                continue
            task = db.get(Task, task_id)
            if task and task.sharing_type == 'public' and task.public_approved == 0:
                task.public_approved = 1
                count += 1
        db.commit()
        flash(f'已批量通过 {count} 个项目公开申请。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-public-audit')


@quickform_bp.route('/admin/public_batch_reject', methods=['POST'])
@admin_required
def admin_public_batch_reject():
    """管理员批量拒绝项目公开申请"""
    task_ids = request.form.getlist('task_ids')
    if not task_ids:
        flash('请先勾选要拒绝的项目', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-public-audit')
    db = SessionLocal()
    try:
        count = 0
        for tid in task_ids:
            try:
                task_id = int(tid)
            except (ValueError, TypeError):
                continue
            task = db.get(Task, task_id)
            if task and task.sharing_type == 'public' and task.public_approved == 0:
                task.public_approved = -1
                count += 1
        db.commit()
        flash(f'已批量拒绝 {count} 个项目公开申请。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-public-audit')


@quickform_bp.route('/admin/open_source_revoke/<int:task_id>', methods=['POST'])
@admin_required
def admin_open_source_revoke(task_id):
    """管理员取消项目在开源/项目交流的展示"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.public_approved != 1:
            flash('任务不存在或未在项目交流展示', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='open-source'))
        task.public_approved = -1
        db.commit()
        flash(f'已取消项目「{task.title}」在项目交流的展示。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='open-source'))


@quickform_bp.route('/admin/open_source_feature/<int:task_id>', methods=['POST'])
@admin_required
def admin_open_source_feature(task_id):
    """管理员切换项目加精状态"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.public_approved != 1:
            flash('任务不存在或未在项目交流展示', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='open-source'))
        task.is_featured = not task.is_featured
        db.commit()
        status = '加精' if task.is_featured else '取消加精'
        flash(f'已{status}项目「{task.title}」。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='open-source'))


@quickform_bp.route('/admin/tutorials_json/save', methods=['POST'])
@admin_required
def admin_tutorials_json_save():
    """管理员保存开源教程菜单的 JSON 配置（static/tutorials/tutorials.json）"""
    content = (request.form.get('tutorials_json') or '').strip()
    if not content:
        flash('内容不能为空', 'danger')
        return redirect(url_for('quickform.admin_panel', tab='tutorials-edit'))
    try:
        data = json.loads(content)
        if not isinstance(data, list):
            flash('JSON 必须为数组格式', 'danger')
            return redirect(url_for('quickform.admin_panel', tab='tutorials-edit'))
        tutorials_dir = os.path.join(current_app.static_folder, 'tutorials')
        os.makedirs(tutorials_dir, exist_ok=True)
        json_path = os.path.join(tutorials_dir, 'tutorials.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        flash('开源教程链接已保存。', 'success')
    except json.JSONDecodeError as e:
        flash(f'JSON 格式错误：{e}', 'danger')
    except OSError as e:
        logger.exception("写入 tutorials.json 失败")
        flash(f'保存文件失败：{e}', 'danger')
    return redirect(url_for('quickform.admin_panel', tab='tutorials-edit'))


@quickform_bp.route('/admin/change_role/<int:user_id>', methods=['POST'])
@admin_required
def admin_change_role(user_id):
    """修改用户角色"""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            flash('用户不存在', 'danger')
            return redirect(url_for('quickform.admin_panel', tab='users'))
        
        if user.id == current_user.id:
            flash('不能修改自己的角色', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='users'))
        
        if user.role == 'admin':
            user.role = 'user'
            flash(f'已将用户 {user.username} 的权限改为普通用户', 'success')
        else:
            user.role = 'admin'
            flash(f'已将用户 {user.username} 的权限改为管理员', 'success')
        
        db.commit()
    finally:
        db.close()
    
    return redirect(url_for('quickform.admin_panel', tab='users'))

@quickform_bp.route('/admin/set_task_limit/<int:user_id>', methods=['POST'])
@admin_required
def admin_set_task_limit(user_id):
    """设置用户任务创建上限为无限制"""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            flash('用户不存在', 'danger')
            return redirect(url_for('quickform.admin_panel', tab='users'))
        
        if user.id == current_user.id:
            flash('不能修改自己的任务上限', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='users'))
        
        if user.role == 'admin':
            flash('管理员用户无需设置任务上限', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='users'))
        
        user.task_limit = -1  # -1表示无限制
        db.commit()
        flash(f'已将用户 {user.username} 的任务创建上限调整为无限制', 'success')
    finally:
        db.close()
    
    return redirect(url_for('quickform.admin_panel', tab='users'))

@quickform_bp.route('/admin/reset_password', methods=['POST'])
@admin_required
def admin_reset_password():
    """管理员重置用户密码为123456"""
    user_id = request.form.get('user_id', type=int)
    if not user_id:
        return jsonify({'success': False, 'message': '缺少用户ID'}), 400
    
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'}), 404
        
        if user.id == current_user.id:
            return jsonify({'success': False, 'message': '不能重置自己的密码'}), 400
        
        # 重置密码为123456
        hashed_password = bcrypt.generate_password_hash('123456').decode('utf-8')
        user.password = hashed_password
        db.commit()
        
        return jsonify({
            'success': True,
            'message': f'用户 {user.username} 的密码已重置为 123456',
            'username': user.username
        })
    except Exception as e:
        db.rollback()
        logger.exception("重置密码失败: %s", e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/admin/users/<int:user_id>/set_email', methods=['POST'])
@admin_required
def admin_set_user_email(user_id):
    """管理员修改用户登录/通知邮箱；修改后该用户需重新验证邮箱。"""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        new_email = (payload.get('new_email') or '').strip()
    else:
        new_email = (request.form.get('new_email') or '').strip()

    db = SessionLocal()
    try:
        target = db.get(User, user_id)
        if not target:
            return jsonify({'success': False, 'message': '用户不存在'}), 404
        actor = db.get(User, current_user.id)
        ok, code = _admin_apply_user_email_change(db, target, actor, new_email)
        if not ok:
            return jsonify({'success': False, 'message': code}), 400
        if code == 'unchanged':
            return jsonify({
                'success': True,
                'message': '邮箱未变化',
                'email': target.email,
                'email_verified': getattr(target, 'email_verified', False),
            }), 200
        db.commit()
        return jsonify({
            'success': True,
            'message': '邮箱已更新，该用户需重新验证邮箱',
            'username': target.username,
            'email': target.email,
            'email_verified': False,
        }), 200
    except Exception as e:
        db.rollback()
        logger.exception("管理员修改用户邮箱失败: %s", e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    """管理员删除用户"""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            flash('用户不存在', 'danger')
            return redirect(url_for('quickform.admin_panel', tab='users'))
        
        if user.id == current_user.id:
            flash('不能删除自己的账号', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='users'))
        
        if user.role == 'admin':
            flash('不能删除管理员账号', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='users'))
        
        username = user.username
        user_id_val = user.id
        
        # 删除用户（级联删除相关数据）
        try:
            db.delete(user)
            db.commit()
            flash(f'已成功删除用户 {username} (ID: {user_id_val}) 及其所有相关数据', 'success')
            logger.info(f"管理员 {current_user.username} 删除了用户 {username} (ID: {user_id_val})")
        except Exception as e:
            db.rollback()
            logger.exception("删除用户失败: %s", e)
            flash('删除用户失败，请查看日志或稍后重试。', 'danger')
            
    finally:
        db.close()
    
    return redirect(url_for('quickform.admin_panel', tab='users'))

@quickform_bp.route('/admin/users/export')
@admin_required
def admin_export_users():
    """导出所有用户数据并生成可视化图表"""
    db = SessionLocal()
    try:
        # 获取所有用户及统计信息
        users = db.query(User).all()
        
        # 准备导出数据
        user_data = []
        for user in users:
            task_count = db.query(Task).filter_by(user_id=user.id).count()
            submission_count = db.query(Submission).join(Task).filter(Task.user_id == user.id).count()
            
            user_data.append({
                'ID': user.id,
                '用户名': user.username,
                '邮箱': user.email,
                '学校': user.school or '',
                '手机': user.phone or '',
                '角色': '管理员' if user.role == 'admin' else '普通用户',
                '认证状态': '已认证' if user.is_certified else '未认证',
                '任务上限': '无限制' if user.task_limit == -1 else user.task_limit,
                '任务数量': task_count,
                '数据提交数': submission_count,
                '注册时间': user.created_at.strftime('%Y-%m-%d %H:%M:%S') if user.created_at else ''
            })
        
        # 生成Excel文件
        df = pd.DataFrame(user_data)
        output = io.BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='用户数据')
        
        output.seek(0)
        
        filename = f"用户数据导出_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        try:
            return send_file(output, download_name=filename, as_attachment=True,
                           mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        except TypeError:
            return send_file(output, attachment_filename=filename, as_attachment=True,
                           mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        logger.exception("导出用户数据失败: %s", e)
        flash('导出用户数据失败，请稍后重试。', 'danger')
        return redirect(url_for('quickform.admin_panel', tab='users'))
    finally:
        db.close()


@quickform_bp.route('/admin/api/daily_registrations')
@admin_required
def admin_api_daily_registrations():
    """管理员接口：按可选时间范围返回每日注册人数，用于可视化"""
    start_s = (request.args.get('start') or '').strip()
    end_s = (request.args.get('end') or '').strip()
    try:
        if start_s:
            start_d = datetime.strptime(start_s, '%Y-%m-%d').date()
        else:
            start_d = (datetime.now() - timedelta(days=30)).date()
        if end_s:
            end_d = datetime.strptime(end_s, '%Y-%m-%d').date()
        else:
            end_d = datetime.now().date()
        if start_d > end_d:
            start_d, end_d = end_d, start_d
    except ValueError:
        start_d = (datetime.now() - timedelta(days=30)).date()
        end_d = datetime.now().date()
    start_dt = datetime.combine(start_d, datetime.min.time())
    end_dt = datetime.combine(end_d, datetime.max.time())
    db = SessionLocal()
    try:
        # 按日期分组统计注册数（兼容 SQLite / MySQL）
        try:
            rows = (
                db.query(func.date(User.created_at).label('day'), func.count(User.id).label('count'))
                .filter(User.created_at >= start_dt, User.created_at <= end_dt)
                .group_by(func.date(User.created_at))
                .order_by(func.date(User.created_at))
                .all()
            )
        except Exception:
            # 部分环境无 date 函数时退化为 Python 聚合
            from collections import defaultdict
            day_count = defaultdict(int)
            for u in db.query(User.created_at).filter(User.created_at >= start_dt, User.created_at <= end_dt).all():
                if u[0]:
                    day_count[u[0].strftime('%Y-%m-%d')] += 1
            rows = [(d, c) for d, c in sorted(day_count.items())]
        data = []
        for r in rows:
            day_val = r[0]
            date_str = day_val.strftime('%Y-%m-%d') if hasattr(day_val, 'strftime') else str(day_val)
            cnt = r[1] if len(r) >= 2 else 0
            data.append({'date': date_str, 'count': int(cnt)})
        return jsonify({'success': True, 'data': data, 'start': start_s or start_d.isoformat(), 'end': end_s or end_d.isoformat()})
    except Exception as e:
        logger.exception('daily_registrations: %s', e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/admin/api/data_stats/<string:section>', methods=['GET'])
@admin_required
def admin_api_data_stats(section: str):
    """管理员接口：按需加载数据报表各分区统计，避免默认全表扫描。"""
    sec = (section or '').strip().lower()
    db = SessionLocal()
    try:
        today = datetime.now().date()
        today_start = datetime.combine(today, datetime.min.time())

        if sec == 'users':
            total_users = db.query(User).count()
            admin_users = db.query(User).filter_by(role='admin').count()
            normal_users = db.query(User).filter_by(role='user').count()
            new_users_today = db.query(User).filter(User.created_at >= today_start).count()
            return jsonify({'success': True, 'section': sec, 'data': {
                'total_users': int(total_users),
                'admin_users': int(admin_users),
                'normal_users': int(normal_users),
                'new_users_today': int(new_users_today),
            }})

        if sec == 'tasks':
            total_users = db.query(User).count()
            total_tasks = db.query(Task).count()
            new_tasks_today = db.query(Task).filter(Task.created_at >= today_start).count()
            avg_tasks_per_user = (total_tasks / total_users) if total_users > 0 else 0
            return jsonify({'success': True, 'section': sec, 'data': {
                'total_tasks': int(total_tasks),
                'new_tasks_today': int(new_tasks_today),
                'avg_tasks_per_user': float(avg_tasks_per_user),
            }})

        if sec == 'submissions':
            total_tasks = db.query(Task).count()
            total_submissions = db.query(Submission).count()
            new_submissions_today = db.query(Submission).filter(Submission.submitted_at >= today_start).count()
            avg_submissions_per_task = (total_submissions / total_tasks) if total_tasks > 0 else 0
            return jsonify({'success': True, 'section': sec, 'data': {
                'total_submissions': int(total_submissions),
                'new_submissions_today': int(new_submissions_today),
                'avg_submissions_per_task': float(avg_submissions_per_task),
            }})

        if sec == 'organizations':
            total_organizations = db.query(Organization).count()
            total_org_members = db.query(OrganizationMember).count()
            tasks_in_organizations = db.query(Task).filter(Task.organization_id.isnot(None)).count()
            return jsonify({'success': True, 'section': sec, 'data': {
                'total_organizations': int(total_organizations),
                'total_org_members': int(total_org_members),
                'tasks_in_organizations': int(tasks_in_organizations),
            }})

        if sec == 'others':
            total_tasks = db.query(Task).count()
            tasks_with_reports = db.query(Task).filter(Task.analysis_report.isnot(None)).count()
            report_generation_rate = (tasks_with_reports / total_tasks * 100) if total_tasks > 0 else 0
            certified_users = db.query(User).filter(User.is_certified == True).count()
            public_tasks = db.query(Task).filter(Task.sharing_type == 'public').count()
            public_approved_tasks = db.query(Task).filter(Task.sharing_type == 'public', Task.public_approved == 1).count()
            total_task_shares = db.query(TaskShare).count()
            total_task_likes = db.query(TaskLike).count()
            ai_generated_tasks = db.query(Task).filter(Task.ai_generated == True).count()
            cert_requests_pending = db.query(CertificationRequest).filter(CertificationRequest.status == 0).count()
            total_posts = db.query(Post).count()
            total_post_replies = db.query(PostReply).count()
            return jsonify({'success': True, 'section': sec, 'data': {
                'tasks_with_reports': int(tasks_with_reports),
                'report_generation_rate': float(report_generation_rate),
                'certified_users': int(certified_users),
                'public_tasks': int(public_tasks),
                'public_approved_tasks': int(public_approved_tasks),
                'total_task_shares': int(total_task_shares),
                'total_task_likes': int(total_task_likes),
                'ai_generated_tasks': int(ai_generated_tasks),
                'cert_requests_pending': int(cert_requests_pending),
                'total_posts': int(total_posts),
                'total_post_replies': int(total_post_replies),
            }})

        return jsonify({'success': False, 'message': '未知统计分区'}), 400
    except Exception as e:
        logger.exception('admin_api_data_stats(%s) failed: %s', sec, e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/admin/projects/top_usage/export')
@admin_required
def admin_export_top_project_usage():
    """导出项目高消耗 TopX（提交数/占用空间/ /all 调用）"""
    sort_by = (request.args.get('sort_by') or 'submissions').strip().lower()
    limit = request.args.get('limit', 20, type=int)
    file_format = (request.args.get('format') or 'xlsx').strip().lower()

    db = SessionLocal()
    try:
        rows = get_top_projects(db, limit=limit, sort_by=sort_by)
        if not rows:
            return jsonify({'success': False, 'message': '暂无可导出的项目数据'}), 404

        export_rows = []
        for idx, r in enumerate(rows, start=1):
            export_rows.append({
                '排名': idx,
                '项目ID': r['task_id'],
                '项目标题': r['task_title'],
                '负责人': r['owner_username'],
                '总提交数': r['submit_count'],
                '近24小时提交数': r['submissions_24h'],
                '近1小时/all调用数': r['all_calls_1h'],
                '数据体积(MB)': r['submission_mb'],
                '文件体积(MB)': r['file_mb'],
                '总占用(MB)': r['total_mb'],
            })

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        df = pd.DataFrame(export_rows)
        output = io.BytesIO()

        if file_format == 'csv':
            csv_text = df.to_csv(index=False)
            output.write(csv_text.encode('utf-8-sig'))
            output.seek(0)
            filename = f"项目高消耗Top{len(export_rows)}_{sort_by}_{ts}.csv"
            mime = 'text/csv; charset=utf-8'
        else:
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='top_usage')
            output.seek(0)
            filename = f"项目高消耗Top{len(export_rows)}_{sort_by}_{ts}.xlsx"
            mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

        try:
            return send_file(output, download_name=filename, as_attachment=True, mimetype=mime)
        except TypeError:
            return send_file(output, attachment_filename=filename, as_attachment=True, mimetype=mime)
    except Exception as e:
        logger.exception("导出项目高消耗Top失败: %s", e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


@quickform_bp.route('/admin/api/projects/alerts/check', methods=['GET'])
@admin_required
def admin_check_project_alerts():
    """检查特定项目预警"""
    monitor_task_ids_raw = (request.args.get('task_ids') or os.getenv('PROJECT_ALERT_TASK_IDS', '')).strip()
    monitor_task_ids = [x.strip() for x in monitor_task_ids_raw.split(',') if x.strip()]

    config = {
        'all_calls_1h': request.args.get('all_calls_1h', int(os.getenv('PROJECT_ALERT_ALL_CALLS_1H', '800')), type=int),
        'all_calls_1h_p1': request.args.get('all_calls_1h_p1', int(os.getenv('PROJECT_ALERT_ALL_CALLS_1H_P1', '2000')), type=int),
        'submissions_24h': request.args.get('submissions_24h', int(os.getenv('PROJECT_ALERT_SUBMISSIONS_24H', '200')), type=int),
        'total_bytes': request.args.get('total_mb', int(os.getenv('PROJECT_ALERT_TOTAL_MB', '2048')), type=int) * 1024 * 1024,
    }

    db = SessionLocal()
    try:
        rows = get_top_projects(db, limit=500, sort_by='all_calls')
        if monitor_task_ids:
            rows = [r for r in rows if r.get('task_id') in monitor_task_ids]
        alerts = evaluate_project_alerts(rows, config)
        return jsonify({
            'success': True,
            'monitored_task_count': len(rows),
            'alert_count': len(alerts),
            'thresholds': {
                'all_calls_1h': config['all_calls_1h'],
                'all_calls_1h_p1': config['all_calls_1h_p1'],
                'submissions_24h': config['submissions_24h'],
                'total_mb': int(config['total_bytes'] / 1024 / 1024),
            },
            'alerts': alerts,
        })
    except Exception as e:
        logger.exception("检查项目预警失败: %s", e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()


# 学校数据提取函数（从参考文件复制）
CITY_TO_PROVINCE = {
    # 直辖市
    '北京': '北京市', '天津': '天津市', '上海': '上海市', '重庆': '重庆市',
    # 河北省
    '石家庄': '河北省', '唐山': '河北省', '秦皇岛': '河北省', '邯郸': '河北省',
    '邢台': '河北省', '保定': '河北省', '张家口': '河北省', '承德': '河北省',
    '沧州': '河北省', '廊坊': '河北省', '衡水': '河北省',
    # 山西省
    '太原': '山西省', '大同': '山西省', '阳泉': '山西省', '长治': '山西省',
    '晋城': '山西省', '朔州': '山西省', '晋中': '山西省', '运城': '山西省',
    '忻州': '山西省', '临汾': '山西省', '吕梁': '山西省',
    # 内蒙古自治区
    '呼和浩特': '内蒙古自治区', '包头': '内蒙古自治区', '乌海': '内蒙古自治区',
    '赤峰': '内蒙古自治区', '通辽': '内蒙古自治区', '鄂尔多斯': '内蒙古自治区',
    '呼伦贝尔': '内蒙古自治区', '巴彦淖尔': '内蒙古自治区', '乌兰察布': '内蒙古自治区',
    '东胜': '内蒙古自治区', '准格尔': '内蒙古自治区',
    # 辽宁省
    '沈阳': '辽宁省', '大连': '辽宁省', '鞍山': '辽宁省', '抚顺': '辽宁省',
    '本溪': '辽宁省', '丹东': '辽宁省', '锦州': '辽宁省', '营口': '辽宁省',
    '阜新': '辽宁省', '辽阳': '辽宁省', '盘锦': '辽宁省', '铁岭': '辽宁省',
    '朝阳': '辽宁省', '葫芦岛': '辽宁省',
    # 吉林省
    '长春': '吉林省', '吉林': '吉林省', '四平': '吉林省', '辽源': '吉林省',
    '通化': '吉林省', '白山': '吉林省', '松原': '吉林省', '白城': '吉林省',
    # 黑龙江省
    '哈尔滨': '黑龙江省', '齐齐哈尔': '黑龙江省', '鸡西': '黑龙江省', '鹤岗': '黑龙江省',
    '双鸭山': '黑龙江省', '大庆': '黑龙江省', '伊春': '黑龙江省', '佳木斯': '黑龙江省',
    '七台河': '黑龙江省', '牡丹江': '黑龙江省', '黑河': '黑龙江省', '绥化': '黑龙江省',
    # 江苏省
    '南京': '江苏省', '无锡': '江苏省', '徐州': '江苏省', '常州': '江苏省',
    '苏州': '江苏省', '南通': '江苏省', '连云港': '江苏省', '淮安': '江苏省',
    '盐城': '江苏省', '扬州': '江苏省', '镇江': '江苏省', '泰州': '江苏省',
    '宿迁': '江苏省', '吴江': '江苏省', '昆山': '江苏省', '太仓': '江苏省',
    '常熟': '江苏省', '张家港': '江苏省', '江阴': '江苏省', '宜兴': '江苏省',
    # 浙江省
    '杭州': '浙江省', '宁波': '浙江省', '温州': '浙江省', '嘉兴': '浙江省',
    '湖州': '浙江省', '绍兴': '浙江省', '金华': '浙江省', '衢州': '浙江省',
    '舟山': '浙江省', '台州': '浙江省', '丽水': '浙江省',
    '乐清': '浙江省', '瑞安': '浙江省', '平阳': '浙江省', '苍南': '浙江省',
    '永嘉': '浙江省', '瓯海': '浙江省', '龙湾': '浙江省', '鹿城': '浙江省',
    '三门': '浙江省', '临海': '浙江省', '义乌': '浙江省',
    '萧山': '浙江省', '余杭': '浙江省', '富阳': '浙江省', '临安': '浙江省',
    # 安徽省
    '合肥': '安徽省', '芜湖': '安徽省', '蚌埠': '安徽省', '淮南': '安徽省',
    '马鞍山': '安徽省', '淮北': '安徽省', '铜陵': '安徽省', '安庆': '安徽省',
    '黄山': '安徽省', '滁州': '安徽省', '阜阳': '安徽省', '宿州': '安徽省',
    '六安': '安徽省', '亳州': '安徽省', '池州': '安徽省', '宣城': '安徽省',
    # 福建省
    '福州': '福建省', '厦门': '福建省', '莆田': '福建省', '三明': '福建省',
    '泉州': '福建省', '漳州': '福建省', '南平': '福建省', '龙岩': '福建省',
    '宁德': '福建省', '晋江': '福建省', '石狮': '福建省', '南安': '福建省',
    # 江西省
    '南昌': '江西省', '景德镇': '江西省', '萍乡': '江西省', '九江': '江西省',
    '新余': '江西省', '鹰潭': '江西省', '赣州': '江西省', '吉安': '江西省',
    '宜春': '江西省', '抚州': '江西省', '上饶': '江西省',
    # 山东省
    '济南': '山东省', '青岛': '山东省', '淄博': '山东省', '枣庄': '山东省',
    '东营': '山东省', '烟台': '山东省', '潍坊': '山东省', '济宁': '山东省',
    '泰安': '山东省', '威海': '山东省', '日照': '山东省', '临沂': '山东省',
    '德州': '山东省', '聊城': '山东省', '滨州': '山东省', '菏泽': '山东省',
    '莱州': '山东省', '荣成': '山东省', '诸城': '山东省', '寿光': '山东省',
    '龙口': '山东省', '莱西': '山东省', '平度': '山东省', '胶州': '山东省',
    # 河南省
    '郑州': '河南省', '开封': '河南省', '洛阳': '河南省', '平顶山': '河南省',
    '安阳': '河南省', '鹤壁': '河南省', '新乡': '河南省', '焦作': '河南省',
    '濮阳': '河南省', '许昌': '河南省', '漯河': '河南省', '三门峡': '河南省',
    '南阳': '河南省', '商丘': '河南省', '信阳': '河南省', '周口': '河南省',
    '驻马店': '河南省',
    # 湖北省
    '武汉': '湖北省', '黄石': '湖北省', '十堰': '湖北省', '宜昌': '湖北省',
    '襄阳': '湖北省', '鄂州': '湖北省', '荆门': '湖北省', '孝感': '湖北省',
    '荆州': '湖北省', '黄冈': '湖北省', '咸宁': '湖北省', '随州': '湖北省',
    # 湖南省
    '长沙': '湖南省', '株洲': '湖南省', '湘潭': '湖南省', '衡阳': '湖南省',
    '邵阳': '湖南省', '岳阳': '湖南省', '常德': '湖南省', '张家界': '湖南省',
    '益阳': '湖南省', '郴州': '湖南省', '永州': '湖南省', '怀化': '湖南省',
    '娄底': '湖南省',
    # 广东省
    '广州': '广东省', '韶关': '广东省', '深圳': '广东省', '珠海': '广东省',
    '汕头': '广东省', '佛山': '广东省', '江门': '广东省', '湛江': '广东省',
    '茂名': '广东省', '肇庆': '广东省', '惠州': '广东省', '梅州': '广东省',
    '汕尾': '广东省', '河源': '广东省', '阳江': '广东省', '清远': '广东省',
    '东莞': '广东省', '中山': '广东省', '潮州': '广东省', '揭阳': '广东省',
    '云浮': '广东省',
    # 广西壮族自治区
    '南宁': '广西壮族自治区', '柳州': '广西壮族自治区', '桂林': '广西壮族自治区',
    '梧州': '广西壮族自治区', '北海': '广西壮族自治区', '防城港': '广西壮族自治区',
    '钦州': '广西壮族自治区', '贵港': '广西壮族自治区', '玉林': '广西壮族自治区',
    '百色': '广西壮族自治区', '贺州': '广西壮族自治区', '河池': '广西壮族自治区',
    '来宾': '广西壮族自治区', '崇左': '广西壮族自治区',
    # 海南省
    '海口': '海南省', '三亚': '海南省', '三沙': '海南省', '儋州': '海南省',
    # 四川省
    '成都': '四川省', '自贡': '四川省', '攀枝花': '四川省', '泸州': '四川省',
    '德阳': '四川省', '绵阳': '四川省', '广元': '四川省', '遂宁': '四川省',
    '内江': '四川省', '乐山': '四川省', '南充': '四川省', '眉山': '四川省',
    '宜宾': '四川省', '广安': '四川省', '达州': '四川省', '雅安': '四川省',
    '巴中': '四川省', '资阳': '四川省',
    # 贵州省
    '贵阳': '贵州省', '六盘水': '贵州省', '遵义': '贵州省', '安顺': '贵州省',
    '毕节': '贵州省', '铜仁': '贵州省',
    # 云南省
    '昆明': '云南省', '曲靖': '云南省', '玉溪': '云南省', '保山': '云南省',
    '昭通': '云南省', '丽江': '云南省', '普洱': '云南省', '临沧': '云南省',
    # 西藏自治区
    '拉萨': '西藏自治区', '日喀则': '西藏自治区', '昌都': '西藏自治区',
    '林芝': '西藏自治区', '山南': '西藏自治区', '那曲': '西藏自治区',
    # 陕西省
    '西安': '陕西省', '铜川': '陕西省', '宝鸡': '陕西省', '咸阳': '陕西省',
    '渭南': '陕西省', '延安': '陕西省', '汉中': '陕西省', '榆林': '陕西省',
    '安康': '陕西省', '商洛': '陕西省',
    # 甘肃省
    '兰州': '甘肃省', '嘉峪关': '甘肃省', '金昌': '甘肃省', '白银': '甘肃省',
    '天水': '甘肃省', '武威': '甘肃省', '张掖': '甘肃省', '平凉': '甘肃省',
    '酒泉': '甘肃省', '庆阳': '甘肃省', '定西': '甘肃省', '陇南': '甘肃省',
    # 青海省
    '西宁': '青海省', '海东': '青海省',
    # 宁夏回族自治区
    '银川': '宁夏回族自治区', '石嘴山': '宁夏回族自治区', '吴忠': '宁夏回族自治区',
    '固原': '宁夏回族自治区', '中卫': '宁夏回族自治区',
    # 新疆维吾尔自治区
    '乌鲁木齐': '新疆维吾尔自治区', '克拉玛依': '新疆维吾尔自治区', '吐鲁番': '新疆维吾尔自治区',
    '哈密': '新疆维吾尔自治区', '昌吉': '新疆维吾尔自治区', '博尔塔拉': '新疆维吾尔自治区',
    '巴音郭楞': '新疆维吾尔自治区', '阿克苏': '新疆维吾尔自治区', '克孜勒苏': '新疆维吾尔自治区',
    '喀什': '新疆维吾尔自治区', '和田': '新疆维吾尔自治区', '伊犁': '新疆维吾尔自治区',
    '塔城': '新疆维吾尔自治区', '阿勒泰': '新疆维吾尔自治区',
    # 港澳台
    '香港': '香港特别行政区', '澳门': '澳门特别行政区', '澳門': '澳门特别行政区', '台北': '台湾省', '臺北': '台湾省', '高雄': '台湾省', '新北': '台湾省', '台中': '台湾省', '臺中': '台湾省', '台南': '台湾省', '臺南': '台湾省', '桃园': '台湾省', '桃園': '台湾省',
}

SCHOOL_TYPES = {
    '幼儿园': '幼儿园',
    '小学': '小学',
    '职业|职校|中专|技校|技工|职高': '中职',
    '大学|学院|研究院(?!.*小学|.*中学)': '高校',
    '教研|教师发展|进修|教育研究|教育局|教体局|教委|管理中心': '教研机构',
}

def extract_school_type(school_name):
    """提取学校类型"""
    if not school_name:
        return "其他"
    
    for pattern, type_name in SCHOOL_TYPES.items():
        if re.search(pattern, school_name):
            return type_name
    
    if '中' in school_name:
        if not re.search(r'中心|中专|中等专业|中小学|中职|中英文|中文', school_name):
            return "中学"
    
    return "其他"

def extract_city_and_province(school_name):
    """同时提取城市和省份"""
    if not school_name:
        return "未知", "未知"
    
    province = "未知"
    city = "未知"
    
    sorted_cities = sorted(CITY_TO_PROVINCE.items(), key=lambda x: len(x[0]), reverse=True)
    
    for city_name, prov in sorted_cities:
        if city_name in school_name:
            province = prov
            if province in ['北京市', '上海市', '天津市', '重庆市', '香港特别行政区', '澳门特别行政区']:
                city = province
            else:
                if city_name + '市' in school_name:
                    city = city_name + '市'
                elif city_name + '县' in school_name:
                    city = city_name + '市'
                elif city_name + '区' in school_name:
                    city_match = re.search(r'([\u4e00-\u9fa5]{2,6}?)市.*?' + city_name, school_name)
                    if city_match:
                        city = city_match.group(1) + '市'
                    else:
                        city = city_name + '市'
                else:
                    city = city_name + '市'
            break
    
    if province != "未知":
        return province, city
    
    province_keywords = {
        '河北': '河北省', '山西': '山西省', '辽宁': '辽宁省', '吉林': '吉林省',
        '黑龙江': '黑龙江省', '江苏': '江苏省', '浙江': '浙江省', '安徽': '安徽省',
        '福建': '福建省', '江西': '江西省', '山东': '山东省', '河南': '河南省',
        '湖北': '湖北省', '湖南': '湖南省', '广东': '广东省', '海南': '海南省',
        '四川': '四川省', '贵州': '贵州省', '云南': '云南省', '陕西': '陕西省',
        '甘肃': '甘肃省', '青海': '青海省', '台湾': '台湾省', '台灣': '台湾省', '臺灣': '台湾省',
        '内蒙古': '内蒙古自治区', '广西': '广西壮族自治区', '西藏': '西藏自治区',
        '宁夏': '宁夏回族自治区', '新疆': '新疆维吾尔自治区',
        '北京': '北京市', '天津': '天津市', '上海': '上海市', '重庆': '重庆市',
        '香港': '香港特别行政区', '澳门': '澳门特别行政区', '澳門': '澳门特别行政区',
    }
    
    for key, full_name in province_keywords.items():
        if key in school_name or full_name in school_name:
            province = full_name
            if province in ['北京市', '上海市', '天津市', '重庆市', '香港特别行政区', '澳门特别行政区']:
                city = province
            break
    
    if city == "未知":
        city_match = re.search(r'([\u4e00-\u9fa5]{2,6}?)市', school_name)
        if city_match:
            city = city_match.group(0)
    
    return province, city

def extract_district(school_name, city):
    """提取区县"""
    if not school_name:
        return "未知"
    
    patterns = [
        r'([\u4e00-\u9fa5]{2,6}?)区',
        r'([\u4e00-\u9fa5]{2,6}?)县',
        r'([\u4e00-\u9fa5]{2,6}?)镇',
        r'([\u4e00-\u9fa5]{2,10}?)开发区',
        r'([\u4e00-\u9fa5]{2,6}?)街道',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, school_name)
        if match:
            return match.group(0)
    
    return "未知"

@quickform_bp.route('/admin/users/statistics')
@admin_required
def admin_users_statistics():
    """用户数据统计和可视化页面"""
    db = SessionLocal()
    try:
        # 获取统计数据
        total_users = db.query(User).count()
        admin_users = db.query(User).filter_by(role='admin').count()
        certified_users = db.query(User).filter_by(is_certified=True).count()
        
        # 生成学校数据（包含省份、城市、区县、类型）
        # 说明：省份支持管理员手动覆盖，避免自动解析把台湾等学校归错。
        school_dict = {}  # school_text -> {name, province_auto, province, province_source, city, district, type}
        db_updated = False

        for user in db.query(User).filter(User.school.isnot(None), User.school != '').all():
            school = (user.school or '').strip()
            if not school or school in ['', 'xx', '1', 'wkg'] or len(school) < 2:
                continue

            user_province_source = getattr(user, 'school_province_source', None)
            manual_province = None
            if user_province_source == 'admin' and getattr(user, 'school_province', None):
                manual_province = user.school_province.strip()

            if school not in school_dict:
                province_auto, city = extract_city_and_province(school)
                district = extract_district(school, city)
                school_type = extract_school_type(school)

                effective_province = manual_province if manual_province else province_auto
                province_source = 'admin' if manual_province else 'auto'

                school_dict[school] = {
                    'name': school,
                    'province_auto': province_auto,
                    'province': effective_province,
                    'province_source': province_source,
                    'city': city,
                    'district': district,
                    'type': school_type,
                }

            entry = school_dict[school]

            # 1) 若当前用户有管理员覆盖省份，则覆盖该 school_text 的统计省份
            if manual_province:
                entry['province'] = manual_province
                entry['province_source'] = 'admin'
                if user.school_province != manual_province or user.school_province_source != 'admin':
                    user.school_province = manual_province
                    user.school_province_source = 'admin'
                    db_updated = True
            else:
                # 2) 自动缓存该用户的“省份（auto）”，用于后续页面/导出减少重复解析
                if not getattr(user, 'school_province', None) and entry.get('province_auto') and entry.get('province_auto') != '未知':
                    user.school_province = entry['province_auto']
                    user.school_province_source = 'auto'
                    db_updated = True
        
        if db_updated:
            db.commit()

        school_data = list(school_dict.values())
        
        # 计算统计信息
        total_schools = len(school_data)
        provinces = set([s['province'] for s in school_data if s['province'] != '未知'])
        total_provinces = len(provinces)
        cities = set([s['city'] for s in school_data if s['city'] != '未知'])
        total_cities = len(cities)
        
        return render_template(
            'admin_user_statistics.html',
            total_users=total_users,
            admin_users=admin_users,
            certified_users=certified_users,
            school_data=school_data,
            total_schools=total_schools,
            total_provinces=total_provinces,
            total_cities=total_cities,
            update_date=datetime.now().strftime('%Y年%m月%d日')
        )
    except Exception as e:
        logger.exception("获取统计数据失败: %s", e)
        flash('获取统计数据失败，请稍后重试。', 'danger')
        return redirect(url_for('quickform.admin_panel'))
    finally:
        db.close()


@quickform_bp.route('/admin/users/<int:user_id>/set_school_province', methods=['POST'])
@admin_required
def admin_set_school_province(user_id):
    """管理员手动设置某个用户的学校省份（仅覆盖统计地区用）"""
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user:
            return jsonify({'success': False, 'message': '用户不存在'}), 404

        province = (request.form.get('school_province') or '').strip()

        # 留空则清除管理员覆盖，让统计页回退到自动解析
        if not province:
            user.school_province = None
            user.school_province_source = None
        else:
            user.school_province = province
            user.school_province_source = 'admin'

        db.commit()
        return jsonify({
            'success': True,
            'school_province': user.school_province,
            'school_province_source': user.school_province_source,
        })
    except Exception as e:
        db.rollback()
        logger.exception("设置用户学校省份失败: %s", e)
        return jsonify({'success': False, 'message': MSG_GENERIC}), 500
    finally:
        db.close()

@quickform_bp.route('/admin/review_html')
@admin_required
def admin_review_html():
    """审核中心：HTML页面审核（仅HTML，认证审核已分离）"""
    db = SessionLocal()
    try:
        page = request.args.get('page', 1, type=int)
        if not page or page < 1:
            page = 1
        per_page = 20
        
        # 查询所有HTML任务 - 使用file_name字段匹配更可靠
        tasks_query = db.query(Task).filter(
            Task.file_path.isnot(None),
            Task.file_name.isnot(None),
            (Task.file_name.like('%.html') | Task.file_name.like('%.htm'))
        )
        
        total_tasks = tasks_query.count()
        total_pages = max(math.ceil(total_tasks / per_page), 1) if total_tasks else 1
        if page > total_pages:
            page = total_pages
        
        tasks = (
            tasks_query
            .order_by(Task.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        
        tasks_with_review = []
        pending_html_count = 0
        for task in tasks:
            author = db.get(User, task.user_id)
            approver = db.get(User, task.html_approved_by) if task.html_approved_by else None
            tasks_with_review.append({
                'task': task,
                'author': author,
                'approver': approver
            })
            if task.html_approved != 1:
                pending_html_count += 1

        return render_template(
            'admin_review_html.html',
            tasks_with_review=tasks_with_review,
            pending_html_count=pending_html_count,
            page=page,
            pages=total_pages,
            total=total_tasks,
            per_page=per_page
        )
    finally:
        db.close()

@quickform_bp.route('/admin/review_html/batch', methods=['POST'])
@admin_required
def admin_review_html_batch():
    """批量通过HTML审核"""
    db = SessionLocal()
    try:
        raw_ids = request.form.getlist('task_ids')
        task_ids = []
        for value in raw_ids:
            try:
                task_ids.append(int(value))
            except (TypeError, ValueError):
                continue

        if not task_ids:
            flash('请选择至少一个待审核的任务', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-html-audit')

        tasks = db.query(Task).filter(Task.id.in_(task_ids)).all()
        if not tasks:
            flash('未找到所选任务', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-html-audit')

        updated_count = 0
        for task in tasks:
            if task.html_approved == 1:
                continue
            task.html_approved = 1
            task.html_approved_by = current_user.id
            task.html_approved_at = datetime.now()
            task.html_review_note = None
            updated_count += 1

        if updated_count:
            db.commit()
            flash(f'成功通过 {updated_count} 个任务的HTML页面审核', 'success')
        else:
            db.rollback()
            flash('所选任务均已通过审核，无需重复操作', 'info')
    except Exception as e:
        db.rollback()
        logger.exception("批量HTML审核失败: %s", e)
        flash('批量审核失败，请稍后重试。', 'danger')
    finally:
        db.close()
    
    return redirect(url_for('quickform.admin_review_html'))


@quickform_bp.route('/admin/certification/<int:request_id>/file')
@admin_required
def admin_view_certification_file(request_id):
    """管理员查看认证材料"""
    db = SessionLocal()
    try:
        cert_request = db.get(CertificationRequest, request_id)
        if not cert_request or not cert_request.file_path or not os.path.exists(cert_request.file_path):
            flash('认证材料不存在或已被删除。', 'danger')
            return redirect(url_for('quickform.admin_panel'))
        filename = os.path.basename(cert_request.file_path)
        try:
            return send_file(cert_request.file_path, download_name=filename, as_attachment=False)
        except TypeError:
            return send_file(cert_request.file_path, attachment_filename=filename, as_attachment=False)
    finally:
        db.close()


@quickform_bp.route('/admin/review_certification')
@admin_required
def admin_review_certification():
    """审核中心：教师认证审核（仅认证，HTML审核已分离）"""
    db = SessionLocal()
    try:
        page = request.args.get('page', 1, type=int)
        if not page or page < 1:
            page = 1
        per_page = 20
        
        cert_requests_query = db.query(CertificationRequest)
        total_requests = cert_requests_query.count()
        total_pages = max(math.ceil(total_requests / per_page), 1) if total_requests else 1
        if page > total_pages:
            page = total_pages
        
        cert_requests = (
            cert_requests_query
            .order_by(CertificationRequest.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        # 统计全部待审核数量，而非仅当前页
        pending_cert_count = (
            db.query(CertificationRequest)
            .filter(CertificationRequest.status == 0)
            .count()
        )

        return render_template(
            'admin_review_certification.html',
            cert_requests=cert_requests,
            pending_cert_count=pending_cert_count,
            page=page,
            pages=total_pages,
            total=total_requests,
            per_page=per_page
        )
    finally:
        db.close()

@quickform_bp.route('/admin/certification/<int:request_id>', methods=['POST'])
@admin_required
def admin_handle_certification(request_id):
    """管理员审核教师认证申请"""
    action = request.form.get('action')
    note = (request.form.get('note') or '').strip()

    db = SessionLocal()
    try:
        cert_request = db.get(CertificationRequest, request_id)
        if not cert_request:
            flash('认证申请不存在', 'danger')
            return redirect(url_for('quickform.admin_panel', tab='cert-review'))

        user = cert_request.user
        if not user:
            flash('无法找到申请人信息', 'danger')
            return redirect(url_for('quickform.admin_panel', tab='cert-review'))

        if action == 'approve':
            if cert_request.status == 1:
                flash('该认证申请已通过审核', 'info')
                return redirect(url_for('quickform.admin_panel', tab='cert-review'))

            ok, code = _cert_apply_approve(db, cert_request, current_user.id, note)
            if not ok:
                flash('无法处理该认证申请（申请人数据异常）。', 'danger')
                return redirect(url_for('quickform.admin_panel', tab='cert-review'))

            db.commit()
            flash(f'已通过 {user.username} 的认证申请，任务上限已调整为无限制。', 'success')
        elif action == 'reject':
            if cert_request.status == -1:
                flash('该认证申请已被拒绝', 'info')
                return redirect(url_for('quickform.admin_panel', tab='cert-review'))

            _cert_apply_reject(db, cert_request, current_user.id, note)
            db.commit()
            flash('已拒绝该认证申请。', 'warning')
        else:
            flash('无效的操作类型', 'danger')
    except Exception as e:
        db.rollback()
        logger.exception("认证审核处理失败: %s", e)
        flash('处理失败，请稍后重试。', 'danger')
    finally:
        db.close()

    return redirect(url_for('quickform.admin_panel', tab='cert-review'))


@quickform_bp.route('/admin/certification/batch_approve', methods=['POST'])
@admin_required
def admin_cert_batch_approve():
    """管理员批量通过教师认证申请"""
    request_ids = request.form.getlist('request_ids')
    if not request_ids:
        flash('请先选择要通过的认证申请', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='cert-review'))

    db = SessionLocal()
    success_count = 0
    try:
        for rid in request_ids:
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                continue

            cert_request = db.get(CertificationRequest, rid_int)
            if not cert_request or cert_request.status == 1:
                continue

            user = cert_request.user
            if not user:
                continue

            cert_request.status = 1
            cert_request.reviewed_at = datetime.now()
            cert_request.reviewed_by = current_user.id

            if not user.is_certified:
                user.is_certified = True
                user.certified_at = datetime.now()
            if user.task_limit != -1:
                user.task_limit = -1

            # 自动通过该用户所有待审核的HTML任务
            pending_tasks = db.query(Task).filter(Task.user_id == user.id, Task.html_approved != 1).all()
            for task in pending_tasks:
                task.html_approved = 1
                task.html_approved_by = current_user.id
                task.html_approved_at = datetime.now()
                task.html_review_note = None

            success_count += 1

        db.commit()
        if success_count:
            flash(f'已批量通过 {success_count} 个教师认证申请。', 'success')
        else:
            flash('没有可处理的认证申请。', 'info')
    except Exception as e:
        db.rollback()
        logger.exception("批量通过教师认证申请失败: %s", e)
        flash('批量处理失败，请稍后重试。', 'danger')
    finally:
        db.close()

    return redirect(url_for('quickform.admin_panel', tab='cert-review'))

@quickform_bp.route('/admin/review_html/<int:task_id>', methods=['POST'])
@admin_required
def admin_review_html_action(task_id):
    """HTML文件审核操作"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-html-audit')
        
        action = request.form.get('action')
        note = (request.form.get('note') or '').strip()
        
        # 处理加精/取消加精操作
        if action == 'feature' or action == 'unfeature':
            if action == 'feature':
                task.is_featured = True
                flash('已标记为加精项目', 'success')
            else:
                task.is_featured = False
                flash('已取消加精标记', 'info')
            db.commit()
            return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-html-audit')
        
        if action == 'approve':
            task.html_approved = 1
            task.html_approved_by = current_user.id
            task.html_approved_at = datetime.now()
            task.html_review_note = note if note else None
            db.commit()
            flash(f'已通过任务 "{task.title}" 的HTML文件审核', 'success')
        elif action == 'reject':
            if not note:
                flash('拒绝审核时需要填写原因。', 'danger')
                return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-html-audit')
            task.html_approved = -1
            task.html_approved_by = current_user.id
            task.html_approved_at = datetime.now()
            task.html_review_note = note
            db.commit()
            flash(f'已拒绝任务 "{task.title}" 的HTML文件审核', 'warning')
        else:
            flash('无效的操作', 'danger')
    finally:
        db.close()
    
    return redirect(url_for('quickform.admin_panel', tab='other-review') + '#section-html-audit')

@quickform_bp.route('/task/<int:task_id>/submission/remove', methods=['GET'])
def remove_submission(task_id):
    """删除单条提交数据（支持DELETE与GET降级）"""
    db = SessionLocal()
    client_ip = get_request_client_ip(request)
    submission_id = request.args.get('submission_id', type=int)
    auth_code = _extract_submission_manage_code()

    def make_response(payload, status_code=200):
        resp = jsonify(payload)
        resp.status_code = status_code
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    logger.info(
        f"[remove_submission] GET user={getattr(current_user, 'id', None)} "
        f"task={task_id} submission={submission_id} ip={client_ip} code={'yes' if auth_code else 'no'}"
    )
    try:
        if not _manage_rate_limit_check(task_id):
            return make_response({'success': False, 'message': '操作过于频繁，请稍后再试。', 'detail': '请避免重复点击；等待数秒后再重试。'}, 429)
        task = db.get(Task, task_id)
        if not task:
            return make_response({'success': False, 'message': '任务不存在'}, 404)
        can_edit = _can_manage_task_submissions(db, task, auth_code)
        if not can_edit:
            logger.warning(
                f"[remove_submission] forbidden user={getattr(current_user, 'id', None)} task={task_id}"
            )
            return make_response({'success': False, 'message': '无权删除此任务的数据，请提供任务删改认证码或使用有编辑权限的账号。', 'detail': '如果你在脚本/大模型里操作，请在请求中携带删改认证码（请求头或 edit_code 参数）。'}, 403)
        # 降低 CSRF 风险：若使用登录态权限而非认证码，则要求 XHR 头
        if not auth_code and (request.headers.get('X-Requested-With') or '') != 'XMLHttpRequest':
            return make_response({'success': False, 'message': '非法请求。请在页面内操作，或使用删改认证码调用接口。', 'detail': '登录态删改需要从本页面发起（XHR）。若从外部脚本调用，请改用删改认证码。'}, 400)
        if not submission_id:
            logger.warning(f"[remove_submission] missing submission_id task={task_id}")
            return make_response({'success': False, 'message': '缺少提交ID'}, 400)
        
        submission = db.query(Submission).filter_by(id=submission_id, task_id=task_id).first()
        if not submission:
            logger.warning(
                f"[remove_submission] submission_not_found task={task_id} submission={submission_id}"
            )
            return make_response({'success': False, 'message': '提交不存在'}, 404)
        
        db.delete(submission)
        db.commit()
        _invalidate_task_read_cache(task.task_id)
        _invalidate_task_data_cache(task.id)
        logger.info(
            f"[remove_submission] success user={getattr(current_user, 'id', None)} task={task_id} submission={submission_id}"
        )
        return make_response({'success': True, 'message': '删除成功'})
    except Exception as e:
        db.rollback()
        logger.exception(
            "[remove_submission] error task=%s submission=%s", task_id, submission_id
        )
        return make_response({'success': False, 'message': MSG_GENERIC}, 500)
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/submissions/remove_batch', methods=['POST'])
def remove_submissions_batch(task_id):
    """批量删除提交数据（用于页面多选删除）。"""
    db = SessionLocal()
    client_ip = get_request_client_ip(request)
    auth_code = _extract_submission_manage_code()

    def make_response(payload, status_code=200):
        resp = jsonify(payload)
        resp.status_code = status_code
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    try:
        if not _manage_rate_limit_check(task_id):
            return make_response({'success': False, 'message': '操作过于频繁，请稍后再试。', 'detail': '请避免重复点击；等待数秒后再重试。'}, 429)
        task = db.get(Task, task_id)
        if not task:
            return make_response({'success': False, 'message': '任务不存在'}, 404)

        can_edit = _can_manage_task_submissions(db, task, auth_code)
        if not can_edit:
            logger.warning(
                f"[remove_submissions_batch] forbidden user={getattr(current_user, 'id', None)} task={task_id}"
            )
            return make_response({'success': False, 'message': '无权删除此任务的数据，请提供任务删改认证码或使用有编辑权限的账号。', 'detail': '如果你在脚本/大模型里操作，请在请求中携带删改认证码（请求头或 edit_code 参数）。'}, 403)

        # 降低 CSRF 风险：若使用登录态权限而非认证码，则要求 XHR 头
        if not auth_code and (request.headers.get('X-Requested-With') or '') != 'XMLHttpRequest':
            return make_response({'success': False, 'message': '非法请求。请在页面内操作，或使用删改认证码调用接口。', 'detail': '登录态删改需要从本页面发起（XHR）。若从外部脚本调用，请改用删改认证码。'}, 400)

        submission_ids = []
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            raw_ids = payload.get('submission_ids') or payload.get('ids') or []
            if isinstance(raw_ids, list):
                submission_ids = raw_ids
        if not submission_ids:
            raw_text = (request.form.get('submission_ids') or request.form.get('ids') or '').strip()
            if raw_text:
                submission_ids = [x.strip() for x in raw_text.split(',') if x.strip()]

        ids = []
        for x in submission_ids:
            try:
                v = int(x)
                if v > 0:
                    ids.append(v)
            except Exception:
                continue
        ids = sorted(set(ids))
        if not ids:
            return make_response({'success': False, 'message': '请选择要删除的提交记录。'}, 400)
        if len(ids) > 500:
            return make_response({'success': False, 'message': '一次最多删除 500 条，请分批操作。'}, 400)

        q = db.query(Submission).filter(Submission.task_id == task_id, Submission.id.in_(ids))
        to_del = q.all()
        if not to_del:
            return make_response({'success': False, 'message': '未找到可删除的提交记录。'}, 404)
        for s in to_del:
            db.delete(s)
        db.commit()
        _invalidate_task_read_cache(task.task_id)
        _invalidate_task_data_cache(task.id)
        logger.info(
            "[remove_submissions_batch] success user=%s task=%s count=%s ip=%s code=%s",
            getattr(current_user, 'id', None), task_id, len(to_del), client_ip, 'yes' if auth_code else 'no'
        )
        return make_response({'success': True, 'message': f'已删除 {len(to_del)} 条提交记录', 'deleted': len(to_del)})
    except Exception as e:
        db.rollback()
        logger.exception("[remove_submissions_batch] error task=%s ip=%s", task_id, client_ip)
        return make_response({'success': False, 'message': MSG_GENERIC}, 500)
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/submission/update', methods=['POST'])
def update_submission(task_id):
    """修改单条提交数据（允许编辑权限账号或携带任务删改认证码）。"""
    db = SessionLocal()
    client_ip = get_request_client_ip(request)
    auth_code = _extract_submission_manage_code()

    def make_response(payload, status_code=200):
        resp = jsonify(payload)
        resp.status_code = status_code
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    try:
        if not _manage_rate_limit_check(task_id):
            return make_response({'success': False, 'message': '操作过于频繁，请稍后再试。', 'detail': '请避免重复点击；等待数秒后再重试。'}, 429)
        task = db.get(Task, task_id)
        if not task:
            return make_response({'success': False, 'message': '任务不存在'}, 404)

        can_edit = _can_manage_task_submissions(db, task, auth_code)
        if not can_edit:
            logger.warning(
                "[update_submission] forbidden task=%s ip=%s code=%s",
                task_id, client_ip, 'yes' if auth_code else 'no'
            )
            return make_response({'success': False, 'message': '无权修改此任务的数据，请提供任务删改认证码或使用有编辑权限的账号。', 'detail': '如果你在脚本/大模型里操作，请在请求中携带删改认证码（请求头或 edit_code 参数）。'}, 403)
        # 降低 CSRF 风险：若使用登录态权限而非认证码，则要求 XHR 头
        if not auth_code and (request.headers.get('X-Requested-With') or '') != 'XMLHttpRequest':
            return make_response({'success': False, 'message': '非法请求。请在页面内操作，或使用删改认证码调用接口。', 'detail': '登录态删改需要从本页面发起（XHR）。若从外部脚本调用，请改用删改认证码。'}, 400)

        payload = None
        if request.is_json:
            payload = request.get_json(silent=True) or {}
        if payload is None:
            payload = {}
        if not payload:
            try:
                payload = request.form.to_dict()
            except Exception:
                payload = {}

        try:
            submission_id = int(payload.get('submission_id') or payload.get('id') or 0)
        except Exception:
            submission_id = 0
        if not submission_id:
            return make_response({'success': False, 'message': '缺少提交ID'}, 400)

        submission = db.query(Submission).filter_by(id=submission_id, task_id=task_id).first()
        if not submission:
            return make_response({'success': False, 'message': '提交不存在'}, 404)

        new_data = payload.get('data')
        if new_data is None:
            new_data = payload.get('new_data')
        if new_data is None:
            return make_response({'success': False, 'message': '缺少 data 字段'}, 400)

        # 允许前端传字符串（编辑器文本），或传对象（JSON）
        if isinstance(new_data, (dict, list)):
            data_text = json.dumps(new_data, ensure_ascii=False)
        else:
            data_text = str(new_data)

        if not data_text.strip():
            return make_response({'success': False, 'message': '数据内容不能为空'}, 400)

        submission.data = data_text
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            err_text = str(e) or ''
            is_data_too_long = (
                isinstance(e, DataError)
                or 'Data too long for column' in err_text
                or '1406' in err_text
            )
            if is_data_too_long:
                return make_response({'success': False, 'message': '修改失败：数据过大（当前字段约 60KB 上限），请勿写入图片/Base64大文本。', 'detail': '建议只提交图片链接 URL，不要把 data:image/...base64 直接写进数据字段。'}, 413)
            raise
        _invalidate_task_read_cache(task.task_id)
        _invalidate_task_data_cache(task.id)
        logger.info("[update_submission] success task=%s submission=%s ip=%s", task_id, submission_id, client_ip)
        return make_response({'success': True, 'message': '修改成功'})
    except Exception as e:
        db.rollback()
        logger.exception("[update_submission] error task=%s ip=%s", task_id, client_ip)
        return make_response({'success': False, 'message': MSG_GENERIC}, 500)
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/submissions/clear', methods=['GET'])
def clear_all_submissions(task_id):
    """删除任务的所有提交数据（支持DELETE与GET降级）"""
    db = SessionLocal()
    client_ip = get_request_client_ip(request)
    auth_code = _extract_submission_manage_code()

    def make_response(payload, status_code=200):
        resp = jsonify(payload)
        resp.status_code = status_code
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    logger.info(
        f"[clear_all_submissions] GET user={getattr(current_user, 'id', None)} task={task_id} ip={client_ip} code={'yes' if auth_code else 'no'}"
    )
    task_clear_lock = _get_submission_clear_lock(task_id)
    try:
        with task_clear_lock:
            if not _manage_rate_limit_check(task_id):
                return make_response({'success': False, 'message': '操作过于频繁，请稍后再试。', 'detail': '请避免重复点击；等待数秒后再重试。'}, 429)
            task = db.get(Task, task_id)
            if not task:
                return make_response({'success': False, 'message': '任务不存在'}, 404)
            can_edit = _can_manage_task_submissions(db, task, auth_code)
            if not can_edit:
                logger.warning(
                    f"[clear_all_submissions] forbidden user={getattr(current_user, 'id', None)} task={task_id}"
                )
                return make_response({'success': False, 'message': '无权删除此任务的数据，请提供任务删改认证码或使用有编辑权限的账号。', 'detail': '如果你在脚本/大模型里操作，请在请求中携带删改认证码（请求头或 edit_code 参数）。'}, 403)
            if not auth_code and (request.headers.get('X-Requested-With') or '') != 'XMLHttpRequest':
                return make_response({'success': False, 'message': '非法请求。请在页面内操作，或使用删改认证码调用接口。', 'detail': '登录态删改需要从本页面发起（XHR）。若从外部脚本调用，请改用删改认证码。'}, 400)

            submission_ids = [
                row[0]
                for row in db.query(Submission.id)
                .filter_by(task_id=task_id)
                .order_by(Submission.id.asc())
                .all()
            ]
            count = len(submission_ids)
            logger.info(
                f"[clear_all_submissions] deleting count={count} user={getattr(current_user, 'id', None)} task={task_id}"
            )
            batch_size = max(50, int(os.getenv('MYSQL_DELETE_BATCH_SIZE', '200')))
            max_retries = max(1, int(os.getenv('MYSQL_DELETE_MAX_RETRIES', '3')))
            retry_sleep = float(os.getenv('MYSQL_DELETE_RETRY_SLEEP', '0.3'))
            deleted_total = 0

            for i in range(0, count, batch_size):
                chunk_ids = submission_ids[i:i + batch_size]
                for attempt in range(1, max_retries + 1):
                    try:
                        deleted_rows = (
                            db.query(Submission)
                            .filter(Submission.id.in_(chunk_ids))
                            .delete(synchronize_session=False)
                        )
                        db.commit()
                        deleted_total += deleted_rows
                        break
                    except Exception as e:
                        db.rollback()
                        err_msg = str(getattr(e, 'orig', e)).lower()
                        is_lock_timeout = ('lock wait timeout exceeded' in err_msg) or ('1205' in err_msg)
                        if is_lock_timeout and attempt < max_retries:
                            time.sleep(retry_sleep * attempt)
                            continue
                        raise

            logger.info(
                f"[clear_all_submissions] success user={getattr(current_user, 'id', None)} task={task_id} deleted={deleted_total}"
            )
            _invalidate_task_read_cache(task.task_id)
            _invalidate_task_data_cache(task.id)
            return make_response({'success': True, 'message': f'成功删除 {deleted_total} 条数据'})
    except Exception:
        db.rollback()
        logger.exception("[clear_all_submissions] error task=%s", task_id)
        return make_response({'success': False, 'message': MSG_GENERIC}, 500)
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/submissions/clear_by_range', methods=['GET'])
def clear_submissions_by_date_range(task_id):
    """按提交日期期间删除数据。参数：date_start（YYYY-MM-DD）、date_end（YYYY-MM-DD），均为必填。"""
    from datetime import datetime as dt
    db = SessionLocal()
    date_start_s = request.args.get('date_start', '').strip()
    date_end_s = request.args.get('date_end', '').strip()
    auth_code = _extract_submission_manage_code()

    def make_response(payload, status_code=200):
        resp = jsonify(payload)
        resp.status_code = status_code
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    task_clear_lock = _get_submission_clear_lock(task_id)
    try:
        with task_clear_lock:
            if not _manage_rate_limit_check(task_id):
                return make_response({'success': False, 'message': '操作过于频繁，请稍后再试。', 'detail': '请避免重复点击；等待数秒后再重试。'}, 429)
            task = db.get(Task, task_id)
            if not task:
                return make_response({'success': False, 'message': '任务不存在'}, 404)
            can_edit = _can_manage_task_submissions(db, task, auth_code)
            if not can_edit:
                return make_response({'success': False, 'message': '无权删除此任务的数据，请提供任务删改认证码或使用有编辑权限的账号。', 'detail': '如果你在脚本/大模型里操作，请在请求中携带删改认证码（请求头或 edit_code 参数）。'}, 403)
            if not auth_code and (request.headers.get('X-Requested-With') or '') != 'XMLHttpRequest':
                return make_response({'success': False, 'message': '非法请求。请在页面内操作，或使用删改认证码调用接口。', 'detail': '登录态删改需要从本页面发起（XHR）。若从外部脚本调用，请改用删改认证码。'}, 400)
            if not date_start_s or not date_end_s:
                return make_response({'success': False, 'message': '请填写开始日期和结束日期'}, 400)
            try:
                start_date = dt.strptime(date_start_s, '%Y-%m-%d').date()
                end_date = dt.strptime(date_end_s, '%Y-%m-%d').date()
            except ValueError:
                return make_response({'success': False, 'message': '日期格式应为 YYYY-MM-DD'}, 400)
            if start_date > end_date:
                return make_response({'success': False, 'message': '开始日期不能晚于结束日期'}, 400)

            range_start = datetime.combine(start_date, datetime.min.time())
            range_end = datetime.combine(end_date + timedelta(days=1), datetime.min.time())
            submission_ids = [
                row[0]
                for row in db.query(Submission.id)
                .filter(Submission.task_id == task_id)
                .filter(Submission.submitted_at >= range_start)
                .filter(Submission.submitted_at < range_end)
                .order_by(Submission.id.asc())
                .all()
            ]
            count = len(submission_ids)
            batch_size = max(50, int(os.getenv('MYSQL_DELETE_BATCH_SIZE', '200')))
            max_retries = max(1, int(os.getenv('MYSQL_DELETE_MAX_RETRIES', '3')))
            retry_sleep = float(os.getenv('MYSQL_DELETE_RETRY_SLEEP', '0.3'))
            deleted_total = 0

            for i in range(0, count, batch_size):
                chunk_ids = submission_ids[i:i + batch_size]
                for attempt in range(1, max_retries + 1):
                    try:
                        deleted_rows = (
                            db.query(Submission)
                            .filter(Submission.id.in_(chunk_ids))
                            .delete(synchronize_session=False)
                        )
                        db.commit()
                        deleted_total += deleted_rows
                        break
                    except Exception as e:
                        db.rollback()
                        err_msg = str(getattr(e, 'orig', e)).lower()
                        is_lock_timeout = ('lock wait timeout exceeded' in err_msg) or ('1205' in err_msg)
                        if is_lock_timeout and attempt < max_retries:
                            time.sleep(retry_sleep * attempt)
                            continue
                        raise
            _invalidate_task_read_cache(task.task_id)
            _invalidate_task_data_cache(task.id)
            return make_response({'success': True, 'message': f'已删除该期间内 {deleted_total} 条数据'})
    except Exception:
        db.rollback()
        logger.exception('clear_submissions_by_date_range error')
        return make_response({'success': False, 'message': MSG_GENERIC}, 500)
    finally:
        db.close()


def init_quickform(app, login_manager_instance=None, database_type=None):
    """
    初始化QuickForm Blueprint
    在主应用中调用此函数来设置LoginManager、Bcrypt等
    
    参数:
        app: Flask应用实例
        login_manager_instance: LoginManager实例（可选）
        database_type: 数据库类型，'sqlite' 或 'mysql'（可选，如果指定则强制使用该类型）
    """
    global bcrypt, login_manager, _database_type
    
    # 如果指定了数据库类型，重新初始化数据库连接
    if database_type:
        _database_type = database_type.lower()
        logger.info(f"根据应用配置，切换数据库类型为: {_database_type}")
        _init_database(_database_type)
    
    # 初始化Flask-Bcrypt
    bcrypt = Bcrypt(app)
    
    # 使用传入的LoginManager实例，如果没有则创建新的
    if login_manager_instance:
        login_manager = login_manager_instance
        login_manager.login_view = 'quickform.login'
    else:
        login_manager = LoginManager()
        login_manager.init_app(app)
        login_manager.login_view = 'quickform.login'
    
    # 注意：user_loader将在主应用中统一设置，支持多系统用户
    
    # 执行数据库迁移
    try:
        migrate_database(engine)
    except Exception as e:
        logger.warning(f"数据库迁移警告: {str(e)}")
    
    # 初始化管理员账号
    def init_admin_account():
        db = SessionLocal()
        try:
            admin_username = 'wzkjgz'
            admin_user = db.query(User).filter_by(username=admin_username).first()
            if not admin_user:
                hashed_password = bcrypt.generate_password_hash('wzkjgz123!').decode('utf-8')
                admin_user = User(
                    username=admin_username,
                    email='wzlinmiaoyan@163.com',
                    password=hashed_password,
                    role='admin',
                    school='温州科技高级中学',
                    phone='00000000000'
                )
                db.add(admin_user)
                db.commit()
                logger.info("成功创建管理员账号：wzkjgz")
            elif admin_user.role != 'admin':
                admin_user.role = 'admin'
                admin_user.password = bcrypt.generate_password_hash('wzkjgz123!').decode('utf-8')
                db.commit()
                logger.info("成功更新管理员账号：wzkjgz")
        except Exception as e:
            logger.error(f"初始化管理员账号失败: {str(e)}")
        finally:
            db.close()
    
    try:
        init_admin_account()
    except Exception as e:
        logger.warning(f"初始化管理员账号警告: {str(e)}")
    
    # 确保uploads目录存在
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    if not os.path.exists(os.path.join(UPLOAD_FOLDER, 'reports')):
        os.makedirs(os.path.join(UPLOAD_FOLDER, 'reports'))
    if not os.path.exists(CERTIFICATION_FOLDER):
        os.makedirs(CERTIFICATION_FOLDER)

    # 未在主应用配置 PUBLIC_BASE_URL 时，从环境变量补全（一键生成嵌入 API 根 URL 用）
    try:
        if not (app.config.get('PUBLIC_BASE_URL') or '').strip():
            _pb = (os.getenv('PUBLIC_BASE_URL') or os.getenv('QUICKFORM_PUBLIC_BASE_URL') or '').strip().rstrip('/')
            if _pb:
                app.config['PUBLIC_BASE_URL'] = _pb
    except Exception:
        pass

    global _QUICKFORM_READY
    _QUICKFORM_READY = True
    logger.info("QuickForm Blueprint 初始化完成（ready=1）")


def init_quickform_async(app, login_manager_instance=None, database_type=None):
    """异步初始化 QuickForm：先让 Web 服务可用（维护页兜底），再后台做 DB 迁移与管理员初始化。"""
    global bcrypt, login_manager, _database_type, _QUICKFORM_READY
    _QUICKFORM_READY = False

    # 1) 先同步建立数据库连接（不做迁移），避免后续后台迁移时还没 engine
    if database_type:
        _database_type = database_type.lower()
        logger.info(f"根据应用配置，切换数据库类型为: {_database_type}")
        _init_database(_database_type)

    # 2) 先把 Flask 扩展挂上（不阻塞）
    bcrypt = Bcrypt(app)
    if login_manager_instance:
        login_manager = login_manager_instance
        login_manager.login_view = 'quickform.login'
    else:
        login_manager = LoginManager()
        login_manager.init_app(app)
        login_manager.login_view = 'quickform.login'

    # 3) 后台线程执行迁移/初始化；完成后置 ready=1
    def _bg():
        global _QUICKFORM_READY
        try:
            try:
                migrate_database(engine)
            except Exception as e:
                logger.warning(f"数据库迁移警告: {str(e)}")

            # 初始化管理员账号（与同步 init_quickform 逻辑一致）
            def init_admin_account():
                db = SessionLocal()
                try:
                    admin_username = 'wzkjgz'
                    admin_user = db.query(User).filter_by(username=admin_username).first()
                    if not admin_user:
                        hashed_password = bcrypt.generate_password_hash('wzkjgz123!').decode('utf-8')
                        admin_user = User(
                            username=admin_username,
                            email='wzlinmiaoyan@163.com',
                            password=hashed_password,
                            role='admin',
                            school='温州科技高级中学',
                            phone='00000000000'
                        )
                        db.add(admin_user)
                        db.commit()
                        logger.info("成功创建管理员账号：wzkjgz")
                    elif admin_user.role != 'admin':
                        admin_user.role = 'admin'
                        admin_user.password = bcrypt.generate_password_hash('wzkjgz123!').decode('utf-8')
                        db.commit()
                        logger.info("成功更新管理员账号：wzkjgz")
                except Exception as e:
                    logger.error(f"初始化管理员账号失败: {str(e)}")
                finally:
                    db.close()

            try:
                init_admin_account()
            except Exception as e:
                logger.warning(f"初始化管理员账号警告: {str(e)}")

            # 确保 uploads 目录存在
            try:
                if not os.path.exists(UPLOAD_FOLDER):
                    os.makedirs(UPLOAD_FOLDER)
                if not os.path.exists(os.path.join(UPLOAD_FOLDER, 'reports')):
                    os.makedirs(os.path.join(UPLOAD_FOLDER, 'reports'))
                if not os.path.exists(CERTIFICATION_FOLDER):
                    os.makedirs(CERTIFICATION_FOLDER)
            except Exception:
                pass

            # PUBLIC_BASE_URL 环境变量兜底
            try:
                if not (app.config.get('PUBLIC_BASE_URL') or '').strip():
                    _pb = (os.getenv('PUBLIC_BASE_URL') or os.getenv('QUICKFORM_PUBLIC_BASE_URL') or '').strip().rstrip('/')
                    if _pb:
                        app.config['PUBLIC_BASE_URL'] = _pb
            except Exception:
                pass

            _QUICKFORM_READY = True
            logger.info("QuickForm Blueprint 后台初始化完成（ready=1）")
        except Exception as ex:
            logger.exception("QuickForm 后台初始化失败: %s", ex)
            # 保持 ready=0，继续展示维护页

    try:
        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        logger.info("QuickForm Blueprint 已启动后台初始化线程（ready=0）")
    except Exception as ex:
        logger.exception("启动 QuickForm 后台初始化线程失败: %s", ex)
        # 启动失败则回退同步初始化（至少让服务可用）
        init_quickform(app, login_manager_instance, database_type=database_type)


# ==================== 组织/团队管理路由 ====================

@quickform_bp.route('/teams')
def teams_list():
    """入驻团队：展示全部团队列表，支持搜索与分页；无需登录可访问；点击加入需填写组织代码（需登录）"""
    db = SessionLocal()
    try:
        from typing import Optional

        def _org_desc_preview(md_text: Optional[str], max_len: int = 140) -> str:
            """组织简介（Markdown）转为适合列表展示的短摘要（纯文本）。"""
            raw = (md_text or "").strip()
            if not raw:
                return ""
            try:
                import markdown as _md
                html_text = _md.markdown(raw, extensions=['extra', 'nl2br'])
                # 去掉 HTML 标签，保留可读文本（避免把 Markdown 直接塞到卡片里撑开布局）
                text = re.sub(r"<[^>]+>", "", html_text)
                text = html.unescape(text)
                text = re.sub(r"\s+", " ", text).strip()
            except Exception:
                # 兜底：简易去符号，避免异常导致页面 500
                text = re.sub(r"[`*_>#-]+", " ", raw)
                text = re.sub(r"\s+", " ", text).strip()
            if max_len > 0 and len(text) > max_len:
                return text[:max_len].rstrip() + "…"
            return text

        q = request.args.get('q', '').strip()
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 10
        base = (
            db.query(Organization)
            .outerjoin(User, Organization.creator_id == User.id)
            .filter(
                Organization.teams_public_requested == True,
                Organization.teams_public_approved == 1,
            )
        )
        if q:
            base = base.filter(or_(Organization.name.ilike(f'%{q}%'), User.username.ilike(f'%{q}%')))
        base = base.order_by(Organization.created_at.desc())
        total_count = base.count()
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        orgs = base.offset((page - 1) * per_page).limit(per_page).all()
        creator_ids = {o.creator_id for o in orgs}
        creators = {u.id: u for u in db.query(User).filter(User.id.in_(creator_ids)).all()} if creator_ids else {}
        team_rows = []
        for org in orgs:
            member_count = db.query(OrganizationMember).filter_by(organization_id=org.id).count()
            task_count = db.query(Task).filter_by(organization_id=org.id).count()
            creator = creators.get(org.creator_id)
            team_rows.append({
                'org': org,
                'creator_name': creator.username if creator else '-',
                'member_count': member_count,
                'task_count': task_count,
                'desc_preview': _org_desc_preview(getattr(org, 'description', None)),
            })
        return render_template('teams_list.html',
                             team_rows=team_rows,
                             q=q,
                             page=page,
                             per_page=per_page,
                             total_count=total_count,
                             total_pages=total_pages)
    except Exception as e:
        logger.exception("teams_list 页面渲染失败: %s", e)
        return (
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>团队列表暂时不可用 - QuickForm</title>"
            "<link href='" + url_for('static', filename='css/bootstrap.min.css') + "' rel='stylesheet'>"
            "</head><body><div class='container py-4' style='max-width:720px'>"
            "<h4 class='mb-2'>团队列表暂时不可用</h4>"
            "<p class='text-muted'>服务器遇到异常，请稍后刷新重试。</p>"
            "<pre class='small text-muted' style='white-space:pre-wrap'>" + html.escape(str(e)) + "</pre>"
            "<a class='btn btn-outline-primary btn-sm' href='" + url_for('quickform.index') + "'>返回首页</a>"
            "</div></body></html>",
            500,
            [('Content-Type', 'text/html; charset=utf-8')],
        )
    finally:
        db.close()


@quickform_bp.route('/organization')
@login_required
def organization():
    """组织管理页面"""
    db = SessionLocal()
    try:
        # 我创建的组织
        created_orgs = db.query(Organization).filter_by(creator_id=current_user.id).all()
        
        # 我加入的组织（不包括我创建的）
        joined_orgs = db.query(OrganizationMember).filter(
            OrganizationMember.user_id == current_user.id,
            OrganizationMember.organization_id.notin_([org.id for org in created_orgs])
        ).all()
        
        return render_template('organization.html', 
                             created_orgs=created_orgs,
                             joined_orgs=joined_orgs)
    finally:
        db.close()


@quickform_bp.route('/organization/create', methods=['POST'])
@login_required
def create_organization():
    """创建组织"""
    db = SessionLocal()
    try:
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        
        if not name:
            flash('组织名称不能为空', 'danger')
            return redirect(url_for('quickform.organization'))

        # 生成五位大写字母数字组织代码（保证唯一）
        def _gen_org_code():
            chars = string.ascii_uppercase + string.digits
            return ''.join(secrets.choice(chars) for _ in range(5))
        for _ in range(20):
            org_code = _gen_org_code()
            if db.query(Organization).filter_by(org_code=org_code).first() is None:
                break
        else:
            flash('生成组织代码失败，请稍后重试', 'danger')
            return redirect(url_for('quickform.organization'))

        # 创建组织
        org = Organization(
            name=name,
            description=description if description else None,
            creator_id=current_user.id,
            org_code=org_code
        )
        db.add(org)
        db.flush()  # 获取org.id
        
        # 创建者自动成为组织成员（管理员角色）
        member = OrganizationMember(
            organization_id=org.id,
            user_id=current_user.id,
            role='admin'
        )
        db.add(member)
        db.commit()
        
        flash(f'组织"{name}"创建成功！组织代码：{org.org_code}', 'success')
        return redirect(url_for('quickform.organization'))
    except Exception as e:
        db.rollback()
        logger.error(f"创建组织失败: {str(e)}")
        flash('创建组织失败', 'danger')
        return redirect(url_for('quickform.organization'))
    finally:
        db.close()


@quickform_bp.route('/organization/join', methods=['POST'])
@login_required
def join_organization():
    """加入组织"""
    db = SessionLocal()
    try:
        org_code = request.form.get('org_code', '').strip()
        
        if not org_code:
            flash('组织代码不能为空', 'danger')
            return redirect(url_for('quickform.organization'))
        
        # 查找组织
        org = db.query(Organization).filter_by(org_code=org_code).first()
        if not org:
            flash('组织代码无效', 'danger')
            return redirect(url_for('quickform.organization'))
        
        # 检查是否已经是成员
        existing = db.query(OrganizationMember).filter_by(
            organization_id=org.id,
            user_id=current_user.id
        ).first()
        
        if existing:
            flash(f'您已经是组织"{org.name}"的成员', 'warning')
            return redirect(url_for('quickform.organization'))
        
        # 加入组织
        member = OrganizationMember(
            organization_id=org.id,
            user_id=current_user.id,
            role='member'
        )
        db.add(member)
        db.commit()
        
        flash(f'成功加入组织"{org.name}"', 'success')
        return redirect(url_for('quickform.organization'))
    except Exception as e:
        db.rollback()
        logger.error(f"加入组织失败: {str(e)}")
        flash('加入组织失败', 'danger')
        return redirect(url_for('quickform.organization'))
    finally:
        db.close()


@quickform_bp.route('/organization/<int:org_id>')
@login_required
def organization_detail(org_id):
    """组织详情页面"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org:
            flash('组织不存在', 'danger')
            return redirect(url_for('quickform.organization'))
        
        # 检查用户是否是组织成员
        is_member = db.query(OrganizationMember).filter_by(
            organization_id=org_id,
            user_id=current_user.id
        ).first() is not None
        
        if not is_member and org.creator_id != current_user.id:
            flash('您不是该组织的成员', 'danger')
            return redirect(url_for('quickform.organization'))
        
        # 查询组织任务
        org_tasks = (
            db.query(Task)
            .options(joinedload(Task.author))
            .filter_by(organization_id=org_id)
            .all()
        )
        is_org_admin = org.creator_id == current_user.id or (
            db.query(OrganizationMember).filter_by(
                organization_id=org_id, user_id=current_user.id, role='admin'
            ).first() is not None
        )
        desc_raw = (org.description or '').strip()
        org_description_html = markdown_to_html(desc_raw) if desc_raw else ''
        return render_template('organization_detail.html',
                             organization=org,
                             org_tasks=org_tasks,
                             is_creator=(org.creator_id == current_user.id),
                             is_org_admin=is_org_admin,
                             org_description_html=org_description_html)
    finally:
        db.close()


@quickform_bp.route('/organization/<int:org_id>/request_teams_public', methods=['POST'])
@login_required
def organization_request_teams_public(org_id):
    """申请将组织展示在「入驻团队」首页列表（需管理员审核）"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org:
            flash('组织不存在', 'danger')
            return redirect(url_for('quickform.organization'))
        is_creator = org.creator_id == current_user.id
        member = db.query(OrganizationMember).filter_by(
            organization_id=org_id, user_id=current_user.id
        ).first()
        is_admin = member and member.role == 'admin'
        if not (is_creator or is_admin):
            flash('只有组织创建者或管理员可发起申请', 'danger')
            return redirect(url_for('quickform.organization_detail', org_id=org_id))
        org.teams_public_requested = True
        org.teams_public_approved = 0
        db.commit()
        flash('已发起「首页公开」申请，请等待管理员审核。审核通过后将出现在「入驻团队」列表。', 'success')
        return redirect(url_for('quickform.organization_detail', org_id=org_id))
    except Exception as e:
        db.rollback()
        logger.exception('organization_request_teams_public failed: %s', e)
        flash('操作失败', 'danger')
        return redirect(url_for('quickform.organization_detail', org_id=org_id))
    finally:
        db.close()


@quickform_bp.route('/organization/<int:org_id>/set_teams_internal', methods=['POST'])
@login_required
def organization_set_teams_internal(org_id):
    """改回仅内部交流：不再在入驻团队公开展示"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org:
            flash('组织不存在', 'danger')
            return redirect(url_for('quickform.organization'))
        is_creator = org.creator_id == current_user.id
        member = db.query(OrganizationMember).filter_by(
            organization_id=org_id, user_id=current_user.id
        ).first()
        is_admin = member and member.role == 'admin'
        if not (is_creator or is_admin):
            flash('只有组织创建者或管理员可修改', 'danger')
            return redirect(url_for('quickform.organization_detail', org_id=org_id))
        org.teams_public_requested = False
        org.teams_public_approved = 0
        db.commit()
        flash('已切换为「内部交流」，组织将不再出现在「入驻团队」列表。', 'success')
        return redirect(url_for('quickform.organization_detail', org_id=org_id))
    except Exception as e:
        db.rollback()
        logger.exception('organization_set_teams_internal failed: %s', e)
        flash('操作失败', 'danger')
        return redirect(url_for('quickform.organization_detail', org_id=org_id))
    finally:
        db.close()


@quickform_bp.route('/organization/<int:org_id>/description', methods=['POST'])
@login_required
def update_organization_description(org_id):
    """创建者或组织管理员可更新团队简介（支持 Markdown）"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org:
            flash('团队不存在', 'danger')
            return redirect(url_for('quickform.organization'))
        is_creator = org.creator_id == current_user.id
        member = db.query(OrganizationMember).filter_by(
            organization_id=org_id, user_id=current_user.id
        ).first()
        is_admin = member and member.role == 'admin'
        if not (is_creator or is_admin):
            flash('只有创建者或组织管理员可编辑团队简介', 'danger')
            return redirect(url_for('quickform.organization_detail', org_id=org_id))
        raw = request.form.get('description', '')
        org.description = raw.strip() if raw and raw.strip() else None
        db.commit()
        flash('团队简介已更新', 'success')
        return redirect(url_for('quickform.organization_detail', org_id=org_id))
    except Exception as e:
        db.rollback()
        logger.exception('update_organization_description failed: %s', e)
        flash('更新失败', 'danger')
        return redirect(url_for('quickform.organization_detail', org_id=org_id))
    finally:
        db.close()


@quickform_bp.route('/organization/<int:org_id>/rename', methods=['POST'])
@login_required
def rename_organization(org_id):
    """仅创建者可修改已创建团队名称与描述"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org:
            flash('团队不存在', 'danger')
            return redirect(url_for('quickform.organization'))
        if org.creator_id != current_user.id:
            flash('仅创建者可修改团队名称', 'danger')
            return redirect(url_for('quickform.organization'))
        name = request.form.get('name', '').strip()
        if not name:
            flash('团队名称不能为空', 'danger')
            return redirect(url_for('quickform.organization'))
        org.name = name
        desc = request.form.get('description', '').strip()
        org.description = desc if desc else None
        db.commit()
        flash('团队名称已更新', 'success')
        return redirect(url_for('quickform.organization'))
    except Exception as e:
        db.rollback()
        logger.exception("修改团队名称失败: %s", e)
        flash('修改失败', 'danger')
        return redirect(url_for('quickform.organization'))
    finally:
        db.close()


@quickform_bp.route('/organization/<int:org_id>/delete', methods=['POST'])
@login_required
def delete_organization(org_id):
    """解散组织"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org:
            flash('组织不存在', 'danger')
            return redirect(url_for('quickform.organization'))
        
        if org.creator_id != current_user.id:
            flash('只有创建者可以解散组织', 'danger')
            return redirect(url_for('quickform.organization'))
        
        # 将组织内的任务改为私有任务
        tasks = db.query(Task).filter_by(organization_id=org_id).all()
        for task in tasks:
            task.organization_id = None
            task.sharing_type = 'private'
        
        # 删除组织（成员会级联删除）
        db.delete(org)
        db.commit()
        
        flash('组织已解散', 'success')
        return redirect(url_for('quickform.organization'))
    except Exception as e:
        db.rollback()
        logger.error(f"解散组织失败: {str(e)}")
        flash('解散组织失败', 'danger')
        return redirect(url_for('quickform.organization'))
    finally:
        db.close()


@quickform_bp.route('/organization/<int:org_id>/leave', methods=['POST'])
@login_required
def leave_organization(org_id):
    """退出组织"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org:
            flash('组织不存在', 'danger')
            return redirect(url_for('quickform.organization'))
        
        if org.creator_id == current_user.id:
            flash('创建者不能退出组织，只能解散组织', 'danger')
            return redirect(url_for('quickform.organization_detail', org_id=org_id))
        
        # 删除成员记录
        member = db.query(OrganizationMember).filter_by(
            organization_id=org_id,
            user_id=current_user.id
        ).first()
        
        if member:
            db.delete(member)
            db.commit()
            flash('已退出组织', 'success')
        else:
            flash('您不是该组织的成员', 'warning')
        
        return redirect(url_for('quickform.organization'))
    except Exception as e:
        db.rollback()
        logger.error(f"退出组织失败: {str(e)}")
        flash('退出组织失败', 'danger')
        return redirect(url_for('quickform.organization'))
    finally:
        db.close()


@quickform_bp.route('/organization/<int:org_id>/set_members_can_edit', methods=['POST'])
@login_required
def set_org_members_can_edit(org_id):
    """组织管理员设置：成员对组织内任务的权限（只读/编辑）。仅组织创建者或组织管理员可操作。"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org:
            flash('组织不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        is_creator = org.creator_id == current_user.id
        member = db.query(OrganizationMember).filter_by(
            organization_id=org_id, user_id=current_user.id
        ).first()
        is_admin = member and member.role == 'admin'
        if not (is_creator or is_admin):
            flash('只有组织创建者或管理员可修改此设置', 'danger')
            return redirect(request.referrer or url_for('quickform.dashboard'))
        value = request.form.get('value', '0').strip()
        org.members_can_edit_tasks = (value == '1' or value == 'true' or value == 'on')
        db.commit()
        flash('组织成员权限已更新：' + ('可编辑组织内任务' if org.members_can_edit_tasks else '仅只读'), 'success')
        return redirect(request.referrer or url_for('quickform.dashboard'))
    except Exception as e:
        db.rollback()
        logger.error(f"设置组织成员权限失败: {str(e)}")
        flash('操作失败', 'danger')
        return redirect(request.referrer or url_for('quickform.dashboard'))
    finally:
        db.close()


@quickform_bp.route('/organization/member/<int:member_id>/remove', methods=['POST'])
@login_required
def remove_member(member_id):
    """移除组织成员"""
    db = SessionLocal()
    try:
        member = db.get(OrganizationMember, member_id)
        if not member:
            return jsonify({'success': False, 'message': '成员不存在'})
        
        org = member.organization
        if org.creator_id != current_user.id:
            return jsonify({'success': False, 'message': '只有创建者可以移除成员'})
        
        if member.user_id == org.creator_id:
            return jsonify({'success': False, 'message': '不能移除创建者'})
        
        db.delete(member)
        db.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.rollback()
        logger.error(f"移除成员失败: {str(e)}")
        return jsonify({'success': False, 'message': '操作失败'})
    finally:
        db.close()


# ==================== 任务共享路由 ====================

@quickform_bp.route('/task/<int:task_id>/assign-to-org', methods=['POST'])
@login_required
def assign_task_to_org(task_id):
    """将任务分配到组织"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        if task.user_id != current_user.id:
            flash('只有任务创建者可以分配任务到组织', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        org_id = request.form.get('organization_id')
        if not org_id:
            flash('请选择组织', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        org_id = int(org_id)
        org = db.get(Organization, org_id)
        if not org:
            flash('组织不存在', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        # 检查用户是否是组织成员
        is_member = db.query(OrganizationMember).filter_by(
            organization_id=org_id,
            user_id=current_user.id
        ).first() is not None
        
        if not is_member and org.creator_id != current_user.id:
            flash('您不是该组织的成员', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        # 分配任务到组织
        task.organization_id = org_id
        task.sharing_type = 'organization'
        db.commit()
        
        flash(f'任务已分配到组织"{org.name}"', 'success')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    except Exception as e:
        db.rollback()
        logger.error(f"分配任务到组织失败: {str(e)}")
        flash('操作失败', 'danger')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/remove-from-org', methods=['POST'])
@login_required
def remove_task_from_org(task_id):
    """将任务从组织中移除"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            return jsonify({'success': False, 'message': '任务不存在'})
        
        if task.user_id != current_user.id:
            return jsonify({'success': False, 'message': '只有任务创建者可以移除组织分配'})
        
        task.organization_id = None
        task.sharing_type = 'private'
        db.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.rollback()
        logger.error(f"移除组织分配失败: {str(e)}")
        return jsonify({'success': False, 'message': '操作失败'})
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/share-to-user', methods=['POST'])
@login_required
def share_task_to_user(task_id):
    """将任务共享给指定用户"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            flash('任务不存在', 'danger')
            return redirect(url_for('quickform.dashboard'))
        
        if task.user_id != current_user.id:
            flash('只有任务创建者可以共享任务', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        username = request.form.get('username', '').strip()
        if not username:
            flash('请输入用户名', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        # 查找用户
        target_user = db.query(User).filter_by(username=username).first()
        if not target_user:
            flash(f'用户"{username}"不存在', 'danger')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        if target_user.id == current_user.id:
            flash('不能共享给自己', 'warning')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        # 检查是否已经共享
        existing = db.query(TaskShare).filter_by(
            task_id=task_id,
            user_id=target_user.id
        ).first()
        
        if existing:
            flash(f'已经共享给用户"{username}"', 'warning')
            return redirect(url_for('quickform.task_detail', task_id=task_id))
        
        # 权限：只读（默认）或 编辑
        share_permission = request.form.get('share_permission', 'readonly')
        can_edit = (share_permission == 'edit')
        share = TaskShare(
            task_id=task_id,
            user_id=target_user.id,
            can_edit=can_edit
        )
        db.add(share)
        
        # 更新任务共享状态
        if task.sharing_type == 'private':
            task.sharing_type = 'shared'
        
        db.commit()
        
        perm_text = '编辑' if can_edit else '只读'
        flash(f'已共享给用户"{username}"（{perm_text}权限）', 'success')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    except Exception as e:
        db.rollback()
        logger.error(f"共享任务失败: {str(e)}")
        flash('操作失败', 'danger')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    finally:
        db.close()


@quickform_bp.route('/task/share/<int:share_id>/remove', methods=['POST'])
@login_required
def remove_task_share(share_id):
    """取消任务共享"""
    db = SessionLocal()
    try:
        share = db.get(TaskShare, share_id)
        if not share:
            return jsonify({'success': False, 'message': '共享记录不存在'})
        
        task = share.task
        if task.user_id != current_user.id:
            return jsonify({'success': False, 'message': '只有任务创建者可以取消共享'})
        
        db.delete(share)
        
        # 检查是否还有其他共享，如果没有了，改为私有
        remaining_shares = db.query(TaskShare).filter_by(task_id=task.id).count()
        if remaining_shares == 0 and task.sharing_type == 'shared':
            task.sharing_type = 'private'
        
        db.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.rollback()
        logger.error(f"取消共享失败: {str(e)}")
        return jsonify({'success': False, 'message': '操作失败'})
    finally:
        db.close()


