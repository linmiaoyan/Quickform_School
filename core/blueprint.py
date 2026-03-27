"""
QuickForm Blueprint
将QuickForm改造为Blueprint，可以整合到主应用中
"""
import os
import json
import math
import random
import re
import secrets
import string
import threading
import html
import base64
import uuid
from urllib.parse import unquote_plus, quote as url_quote
import zipfile
from flask import Blueprint, render_template, redirect, url_for, request, flash, jsonify, make_response, send_file, send_from_directory, current_app
from sqlalchemy import create_engine, or_, text, func
from sqlalchemy.orm import sessionmaker
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
from typing import Deque
import time
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

# 导入分离的模块
from .models import Base, User, Task, Submission, AIConfig, migrate_database, CertificationRequest, Post, PostReply, Organization, OrganizationMember, TaskShare, TaskLike
from .secret_store import decrypt_ai_config_inplace, encrypt_ai_config_inplace
from services.file_service import save_uploaded_file, read_file_content, ALLOWED_EXTENSIONS, allowed_file, CERTIFICATION_ALLOWED_EXTENSIONS
from services.ai_service import call_ai_model, generate_analysis_prompt, analyze_html_file, generate_html_page_from_prompt, revise_html_with_ai
from services.report_service import (
    save_analysis_report, generate_report_image, build_report_html, perform_analysis_with_custom_prompt,
    analysis_progress, analysis_results, completed_reports, progress_lock, timeout, markdown_to_html
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

# 加载环境变量
load_dotenv()


def _is_placeholder_or_empty_email(email):
    """未填邮箱或占位邮箱（注册时未填则存 username@noreply.local）视为未绑定"""
    if not email or not (email or '').strip():
        return True
    return (email or '').strip().endswith('@noreply.local')


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
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '') or 'unknown'
    ip = ip.split(',')[0].strip()
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


def send_email_code(to_email: str, code: str):
    """发送邮箱验证码"""
    try:
        conf = current_app.config
        sender = conf.get('MAIL_USERNAME')
        if not sender or not conf.get('MAIL_PASSWORD'):
            logger.error("邮件配置不完整，无法发送验证码")
            raise RuntimeError("邮件配置不完整")

        sender_name = "QuickForm 验证码"
        # 统一使用中性的标题，适用于注册、重置密码等场景
        subject = "QuickForm 验证码"
        body = (
            f"您的验证码是：{code}，有效期 10 分钟。\n\n"
            f"如果不是您本人在 QuickForm 中发起的操作，请忽略此邮件。"
        )

        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = formataddr((sender_name, sender))
        msg['To'] = to_email
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
            server.sendmail(sender, [to_email], msg.as_string())
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

        logger.info(f"验证码邮件已发送至: {to_email}")
    except Exception as e:
        # 统一在此处记录详细日志，但对外抛出简单错误信息，避免暴露内部实现细节
        logger.exception(f"发送邮箱验证码失败: {e}")
        raise


# 获取QuickForm目录路径
QUICKFORM_DIR = os.path.dirname(os.path.abspath(__file__))

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
    
    # 初始化SQLAlchemy引擎
    mysql_connection_failed = False
    if DATABASE_URL.startswith('mysql'):
        # MySQL连接配置
        try:
            engine = create_engine(
                DATABASE_URL,
                pool_pre_ping=True,  # 自动重连
                pool_recycle=3600,   # 连接回收时间
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
    
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

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


@quickform_bp.context_processor
def inject_upload_url():
    return dict(get_upload_file_url=get_upload_file_url)

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
    
    return render_template('home.html', video_files=video_files, partner_logos=partner_logos)

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
    """项目交流 - 留言板 + 公开项目展示（最新发布、最高点赞），支持分页"""
    db = SessionLocal()
    try:
        per_project = max(1, min(20, request.args.get('per_project', 8, type=int)))
        per_post = max(1, min(50, request.args.get('per_post', 10, type=int)))
        page_latest = max(1, request.args.get('latest_page', 1, type=int))
        page_liked = max(1, request.args.get('liked_page', 1, type=int))
        page_posts = max(1, request.args.get('post_page', 1, type=int))

        base_public = db.query(Task).filter(Task.sharing_type == 'public', Task.public_approved == 1)
        total_latest = base_public.count()
        total_liked = total_latest  # 同一批数据不同排序
        public_tasks_latest = (
            base_public.order_by(Task.created_at.desc())
            .offset((page_latest - 1) * per_project)
            .limit(per_project)
            .all()
        )
        public_tasks_liked = (
            db.query(Task)
            .filter(Task.sharing_type == 'public', Task.public_approved == 1)
            .order_by(func.coalesce(Task.like_count, 0).desc(), Task.created_at.desc())
            .offset((page_liked - 1) * per_project)
            .limit(per_project)
            .all()
        )
        pages_latest = max(1, (total_latest + per_project - 1) // per_project) if total_latest else 1
        pages_liked = max(1, (total_liked + per_project - 1) // per_project) if total_liked else 1

        posts_query = db.query(Post).order_by(Post.created_at.desc())
        total_posts = posts_query.count()
        posts = posts_query.offset((page_posts - 1) * per_post).limit(per_post).all()
        pages_posts = max(1, (total_posts + per_post - 1) // per_post) if total_posts else 1

        pagination_latest = {'page': page_latest, 'per_page': per_project, 'pages': pages_latest, 'total': total_latest}
        pagination_liked = {'page': page_liked, 'per_page': per_project, 'pages': pages_liked, 'total': total_liked}
        pagination_posts = {'page': page_posts, 'per_page': per_post, 'pages': pages_posts, 'total': total_posts}

        return render_template(
            'community.html',
            posts=posts,
            public_tasks_latest=public_tasks_latest,
            public_tasks_liked=public_tasks_liked,
            pagination_latest=pagination_latest,
            pagination_liked=pagination_liked,
            pagination_posts=pagination_posts
        )
    finally:
        db.close()

@quickform_bp.route('/community/post', methods=['POST'])
@login_required
def create_post():
    """创建留言"""
    content = request.form.get('content', '').strip()
    if not content:
        flash('留言内容不能为空', 'danger')
        return redirect(url_for('quickform.community'))
    
    if len(content) > 2000:
        flash('留言内容过长，最多2000字符', 'danger')
        return redirect(url_for('quickform.community'))
    
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
    
    return redirect(url_for('quickform.community'))

@quickform_bp.route('/community/post/<int:post_id>/reply', methods=['POST'])
@login_required
def create_reply(post_id):
    """针对某条留言发表回复"""
    content = request.form.get('content', '').strip()
    if not content:
        flash('回复内容不能为空', 'danger')
        return redirect(url_for('quickform.community'))
    if len(content) > 2000:
        flash('回复内容过长，最多2000字符', 'danger')
        return redirect(url_for('quickform.community'))
    db = SessionLocal()
    try:
        post = db.get(Post, post_id)
        if not post:
            flash('该留言不存在', 'danger')
            return redirect(url_for('quickform.community'))
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
    return redirect(url_for('quickform.community'))

@quickform_bp.route('/community/reply/<int:reply_id>/delete', methods=['POST'])
@login_required
def delete_reply(reply_id):
    """删除回复（仅管理员）"""
    if not current_user.is_admin():
        flash('无权执行此操作', 'danger')
        return redirect(url_for('quickform.community'))
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
    return redirect(url_for('quickform.community'))

@quickform_bp.route('/task/<int:task_id>/like', methods=['POST'])
@login_required
def task_like(task_id):
    """公开任务点赞/取消点赞（仅登录用户，仅对公开任务）"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
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
        logger.exception(f"点赞操作失败: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        db.close()

@quickform_bp.route('/community/post/<int:post_id>/delete', methods=['POST'])
@login_required
def delete_post(post_id):
    """删除留言（仅管理员）"""
    if not current_user.is_admin():
        flash('无权执行此操作', 'danger')
        return redirect(url_for('quickform.community'))
    
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
    
    return redirect(url_for('quickform.community'))

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
                from sqlalchemy import or_
                conditions = [User.username == username, User.phone == phone]
                if email:
                    conditions.append(User.email == email)
                existing_user = db.query(User).filter(or_(*conditions)).first()
                
                if existing_user:
                    if existing_user.username == username:
                        flash('用户名已存在', 'danger')
                    elif email and existing_user.email == email:
                        flash('邮箱已存在', 'danger')
                    else:
                        flash('手机号已被注册', 'danger')
                    return redirect(url_for('quickform.register'))
                
                hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
                # 邮箱未填时使用唯一占位符，避免多个空字符串违反 unique 约束
                email_value = email if email else f"{username}@noreply.local"
                user = User(username=username, email=email_value, password=hashed_password, 
                           school=school, phone=phone or '')
                
                ai_config = AIConfig(user=user, selected_model='chat_server')
                
                db.add(user)
                db.commit()
                
                flash('注册成功，请登录', 'success')
                return redirect(url_for('quickform.login'))
            finally:
                db.close()
        else:
            # GET请求，显示注册页面
            return render_template('register.html')
    except Exception as e:
        logger.exception("注册页面异常")
        flash(f'页面加载错误: {str(e)}', 'danger')
        try:
            return render_template('register.html')
        except:
            return f"注册页面加载失败: {str(e)}", 500


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
    except Exception as e:
        logger.exception("发送验证码异常")
        return jsonify({'success': False, 'message': str(e) if isinstance(e, RuntimeError) else '发送失败，请稍后重试'}), 500


@quickform_bp.route('/login', methods=['GET', 'POST'])
def login():
    """登录"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = request.form.get('remember') == 'on'
        
        db = SessionLocal()
        try:
            # 尝试多种方式查找用户：用户名、邮箱、手机号、昵称
            user = db.query(User).filter(
                (User.username == username) | 
                (User.email == username) | 
                (User.phone == username)
            ).first()
            
            if user and bcrypt.check_password_hash(user.password, password):
                # 登录成功，清除失败次数
                from flask import session
                session.pop('login_fail_count', None)
                login_user(user, remember=remember)
                # 将会话设为持久，使 session cookie 在 PERMANENT_SESSION_LIFETIME 内有效，重启服务后仍保持登录
                session.permanent = True
                next_page = request.args.get('next')
                return redirect(next_page) if next_page else redirect(url_for('quickform.dashboard'))
            else:
                # 登录失败，检查失败次数
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


@quickform_bp.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    """
    通过手机号确认身份，再向绑定邮箱发送验证码以重置密码。
    第一步：输入手机号，查找账户并展示用户名；
    第二步：发送验证码并重置密码（仅使用服务器端查到的邮箱）。
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
                # 跳转到第二步页面，展示用户名并允许发送验证码与重置密码
                return render_template('forgot_password.html', mode='verify', user=user)
            finally:
                db.close()
        else:
            # 第二步：根据会话中的用户信息校验验证码并重置密码
            email_code = (request.form.get('email_code') or '').strip()
            new_password = (request.form.get('new_password') or '').strip()
            confirm_password = (request.form.get('confirm_password') or '').strip()

            if not email_code or not new_password or not confirm_password:
                flash('请填写所有必填项', 'danger')
                return redirect(url_for('quickform.forgot_password'))

            if new_password != confirm_password:
                flash('两次输入的密码不一致', 'danger')
                return redirect(url_for('quickform.forgot_password'))

            if len(new_password) < 6:
                flash('新密码长度至少为6个字符', 'danger')
                return redirect(url_for('quickform.forgot_password'))

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

                # 校验邮箱验证码（只使用服务器端查到的邮箱）
                if not verify_email_code(user.email, email_code):
                    flash('邮箱验证码错误或已过期，请重新获取', 'danger')
                    return redirect(url_for('quickform.forgot_password'))

                hashed = bcrypt.generate_password_hash(new_password).decode('utf-8')
                user.password = hashed
                db.commit()
                # 完成后清理会话标记
                session.pop('pw_reset_user_id', None)
                flash('密码重置成功，请使用新密码登录', 'success')
                return redirect(url_for('quickform.login'))
            finally:
                db.close()

    # GET 或表单校验失败后默认回到第一步手机号输入页面
    return render_template('forgot_password.html', mode='start', user=None)


@quickform_bp.route('/forgot_password/send_code', methods=['POST'])
def forgot_password_send_code():
    """
    第二步中点击“发送验证码”时调用。
    只根据会话中的用户ID查找邮箱，避免前端伪造邮箱。
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

        code = f"{random.randint(0, 999999):06d}"
        set_email_code(user.email, code, ttl_seconds=600)
        send_email_code(user.email, code)
        return jsonify({'success': True, 'message': '验证码已发送'})
    except Exception as e:
        logger.exception(f"发送重置密码验证码失败: {e}")
        return jsonify({'success': False, 'message': '服务器异常，请稍后再试'}), 500
    finally:
        db.close()


@quickform_bp.route('/forgot_username', methods=['GET', 'POST'])
def forgot_username():
    """通过手机号查询用户名"""
    if request.method == 'POST':
        phone = (request.form.get('phone') or '').strip()
        if not phone:
            flash('请填写手机号', 'danger')
            return redirect(url_for('quickform.forgot_username'))

        db = SessionLocal()
        try:
            users = db.query(User).filter(User.phone == phone).all()
            if not users:
                flash('未找到使用该手机号注册的账户', 'warning')
            else:
                # 如果多个账户共用手机号，一并提示
                usernames = ', '.join(u.username for u in users)
                flash(f'该手机号对应的用户名为：{usernames}', 'info')
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
        own_tasks = db.query(Task).filter_by(user_id=current_user.id).all()
        
        # 2. 用户所在组织的任务
        user_orgs = db.query(OrganizationMember).filter_by(user_id=current_user.id).all()
        org_ids = [m.organization_id for m in user_orgs]
        org_tasks = db.query(Task).filter(Task.organization_id.in_(org_ids)).all() if org_ids else []
        
        # 3. 共享给用户的任务（带权限：只读/编辑）
        shared_records = db.query(TaskShare).filter_by(user_id=current_user.id).all()
        shared_task_ids = [s.task_id for s in shared_records]
        shared_tasks = db.query(Task).filter(Task.id.in_(shared_task_ids)).all() if shared_task_ids else []
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
                org_can_edit = task.organization and getattr(task.organization, 'members_can_edit_tasks', False)
                task_access[task.id] = 'org_edit' if org_can_edit else 'org_readonly'
        for task in shared_tasks:
            if task.id not in all_task_ids:
                all_task_ids.add(task.id)
                tasks.append(task)
                task_access[task.id] = 'shared_edit' if shared_can_edit.get(task.id, False) else 'shared_readonly'
        
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
        return render_template(
            'dashboard.html',
            tasks=tasks,
            own_tasks=own_tasks,
            org_tasks=org_tasks,
            shared_tasks=shared_tasks,
            task_access=task_access,
            task_count=task_count,
            task_limit=task_limit,
            is_certified=is_certified
        )
    finally:
        db.close()


# 一键生成新任务：可勾选追加的说明文案（API地址 会在提交时替换为真实地址）
# 数据获取接口 GET API地址/all 返回格式：{ submissions: 数组, total_submissions: 数字 }，前端应用 data.submissions 或 data.total_submissions，不要用 data.length
ONECLICK_PROMPT_OPTIONS = [
    ('opt_upload', '数据上传', '创建完应用后，向API地址发送post格式的json数据。'),
    ('opt_fetch', '数据获取', 'GET 请求 API地址/all 可获取已提交数据。接口返回 JSON 对象格式为：{ submissions: 数组, total_submissions: 数字 }。前端用 data.submissions 得到记录数组，用 data.total_submissions 得到人数，不要用 response.json() 后直接 .length（因为根对象不是数组）。'),
    ('opt_responsive', '响应式布局', '页面需要响应式布局，适配手机和电脑。'),
    ('opt_validate', '表单校验', '需要表单必填项校验与提交前错误提示。'),
    ('opt_success_tip', '提交成功提示', '提交成功后显示成功提示，并可选清空表单。'),
    ('opt_decorate', '内置页面装饰', '页面需要内置简洁的 CSS 装饰：卡片/表单区域使用圆角、轻微阴影与适当留白，配色清爽，整体美观易读。'),
]


@quickform_bp.route('/oneclick_create_task', methods=['GET', 'POST'])
@login_required
def oneclick_create_task():
    """一键生成新任务（内测）：仅认证教师可用，根据描述生成 HTML 并自动上传到新任务"""
    if not (current_user.is_admin() or getattr(current_user, 'is_certified', False)):
        flash('一键生成新任务仅对认证教师开放，请先完成教师认证。', 'warning')
        return redirect(url_for('quickform.dashboard'))
    if request.method == 'GET':
        return render_template(
            'oneclick_create_task.html',
            prompt_options=ONECLICK_PROMPT_OPTIONS,
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
        refreshed_user = db.get(User, current_user.id)
        # 超过 3 个任务时才要求先绑定并验证邮箱
        if task_count >= 3:
            if _is_placeholder_or_empty_email(refreshed_user.email):
                flash('创建更多数据任务前请先在个人资料中绑定邮箱（修改为您的个人邮箱）。', 'warning')
                return redirect(url_for('quickform.profile', next=url_for('quickform.oneclick_create_task')))
            if not getattr(refreshed_user, 'email_verified', True):
                flash('创建更多数据任务前请先验证邮箱。', 'warning')
                return redirect(url_for('quickform.verify_email', next=url_for('quickform.oneclick_create_task')))
        # 创建新任务以得到 task_id 与 API 地址
        task = Task(title=title, description='', user_id=current_user.id, sharing_type='private')
        db.add(task)
        db.flush()
        new_task_pk = task.id  # 用于成功后跳转任务详情（避免 commit 后 session 状态影响）
        api_base = (request.host_url or request.url_root or '').rstrip('/')
        api_url = f"{api_base}/api/{task.task_id}"
        # 拼接用户需求与勾选说明，作为发给 AI 的完整提示词（仅 full_prompt 用于生成，不入库）
        lines = [requirements]
        for key, _label, text in ONECLICK_PROMPT_OPTIONS:
            if request.form.get(key) == 'on':
                lines.append(text.replace('API地址', api_url))
        full_prompt = '\n\n'.join(lines)
        task.description = requirements  # 任务描述只存用户输入的那段，不包含勾选的预设说明
        # 获取用户 AI 配置。一键内测优先用用户自己在个人中心配置的 API；未配置时直接使用您提供的 API（环境变量 CHAT_SERVER_API_TOKEN）
        ai_config = db.query(AIConfig).filter_by(user_id=current_user.id).first()
        decrypt_ai_config_inplace(ai_config)
        if not ai_config:
            # 用户从未保存过配置：用 chat_server + 空 Token，call_ai_model 会回退到环境变量 CHAT_SERVER_API_TOKEN
            ai_config = AIConfig(user_id=current_user.id, selected_model='chat_server')
        elif ai_config.selected_model == 'chat_server' and not (ai_config.chat_server_api_token or '').strip():
            # 用户选了硅基流动但未填 Token：同样回退到环境变量，无需提醒
            pass
        try:
            html_content = generate_html_page_from_prompt(full_prompt, call_ai_model, ai_config)
        except Exception as e:
            db.rollback()
            logger.exception("一键生成 HTML 失败")
            flash(f'生成 HTML 失败：{str(e)}。请检查 API 配置或稍后重试。', 'danger')
            return redirect(url_for('quickform.oneclick_create_task'))
        # 保存 HTML 到任务（单文件，新文件存 static/uploads 由静态服务提供）
        static_uploads = _static_uploads_dir()
        unique_filename = str(uuid.uuid4()) + '_oneclick.html'
        filepath = os.path.join(static_uploads, unique_filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        task.file_name = 'oneclick.html'
        task.file_path = filepath
        task.html_files = json.dumps([{'original_name': 'oneclick.html', 'saved_name': unique_filename}])
        task.ai_generated = True
        task.html_ai_edit_remaining = 3
        if current_user.is_admin() or getattr(current_user, 'is_certified', False):
            task.html_approved = 1
            task.html_approved_by = current_user.id
            task.html_approved_at = datetime.now()
        else:
            task.html_approved = 0
        db.commit()
        flash('任务已创建，HTML 已生成并上传。您可在任务详情中查看或继续修改（剩余 3 次）。', 'success')
        return redirect(url_for('quickform.task_detail', task_id=new_task_pk))
    except Exception as e:
        db.rollback()
        logger.exception("一键创建任务失败")
        flash(f'创建失败：{str(e)}', 'danger')
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
            # 超过 3 个任务时需先绑定邮箱并验证
            refreshed_user = db.get(User, current_user.id)
            if task_count >= 3:
                email_verified = getattr(refreshed_user, 'email_verified', True)
                if _is_placeholder_or_empty_email(refreshed_user.email):
                    flash('创建更多数据任务前请先在个人资料中绑定邮箱（修改为您的个人邮箱）。', 'warning')
                    return redirect(url_for('quickform.profile', next=url_for('quickform.create_task')))
                if not email_verified:
                    flash('创建更多数据任务前请先验证邮箱。', 'warning')
                    return redirect(url_for('quickform.verify_email', next=url_for('quickform.create_task')))
        
        if request.method == 'POST':
            title = request.form.get('title')
            description = request.form.get('description')
            organization_id = request.form.get('organization_id')
            
            task = Task(title=title, description=description, user_id=current_user.id)
            task.share_url = (request.form.get('share_url') or '').strip() or None
            task.tutorial_link = (request.form.get('tutorial_link') or '').strip() or None
            
            # 设置组织和共享类型（支持：私有、组织、公开；公开仅认证教师或管理员可用）
            share_scope = request.form.get('share_scope', 'private')
            if share_scope == 'public':
                if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                    task.sharing_type = 'public'
                    task.organization_id = None
                else:
                    flash('只有通过教师认证的用户才能公开项目到共享区', 'warning')
                    task.sharing_type = 'private'
                    task.organization_id = None
            elif share_scope == 'organization' and organization_id and organization_id.strip() and organization_id != 'none':
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
                    else:
                        task.sharing_type = 'private'
                except (ValueError, TypeError):
                    task.sharing_type = 'private'
            else:
                if organization_id and organization_id.strip() and organization_id != 'none':
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
                        else:
                            task.sharing_type = 'private'
                    except (ValueError, TypeError):
                        task.sharing_type = 'private'
                else:
                    task.sharing_type = 'private'
            
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
        
        # 获取用户的组织列表
        user_orgs_created = db.query(Organization).filter_by(creator_id=current_user.id).all()
        user_orgs_joined = db.query(OrganizationMember).filter_by(user_id=current_user.id).all()
        user_organizations = user_orgs_created + [m.organization for m in user_orgs_joined if m.organization.id not in [o.id for o in user_orgs_created]]

        return render_template('create_task.html', 
                             task_limit=task_limit, 
                             is_certified=is_certified, 
                             task_count=task_count,
                             user_organizations=user_organizations)
    finally:
        db.close()

@quickform_bp.route('/task/<int:task_id>')
def task_detail(task_id):
    """任务详情（公开任务支持未登录访问，但不显示分析/导出）"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
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
        for f in html_files:
            if isinstance(f, dict) and 'saved_name' in f:
                f['url'] = get_upload_file_url(f['saved_name'], task.file_path)

        # 任务详情页不展示分页列表，pagination 仅用于模板兼容（见上方已赋初值）
        
        # 仅登录用户需要组织列表与共享列表（用于分配任务/共享管理）
        user_organizations = []
        shared_users = []
        if current_user.is_authenticated:
            user_orgs_created = db.query(Organization).filter_by(creator_id=current_user.id).all()
            user_orgs_joined = db.query(OrganizationMember).filter_by(user_id=current_user.id).all()
            user_organizations = user_orgs_created + [m.organization for m in user_orgs_joined if m.organization.id not in [o.id for o in user_orgs_created]]
            shared_users = db.query(TaskShare).filter_by(task_id=task.id).all()

        # 公开项目且当前用户非所有者/管理员等：仅展示任务名称、简介、网页（不展示数据与导出等）
        is_public_visitor = (task.sharing_type == 'public' and not can_analyze_export)

        # 当前用户是否为组织创建者或组织管理员（用于显示组织成员权限开关）
        can_edit_org_settings = False
        if task.organization_id and current_user.is_authenticated and task.organization:
            if task.organization.creator_id == current_user.id:
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
                if org_mem and task.organization and getattr(task.organization, 'members_can_edit_tasks', False):
                    can_edit_task = True
            else:
                share_record = db.query(TaskShare).filter_by(
                    task_id=task.id, user_id=current_user.id
                ).first()
                if share_record and share_record.can_edit:
                    can_edit_task = True

        return render_template(
            'task_detail.html',
            task=task,
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
            is_public_visitor=is_public_visitor
        )
    finally:
        db.close()


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
            if org_mem and task.organization and getattr(task.organization, 'members_can_edit_tasks', False):
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
        total_submissions = submission_query.count()
        total_pages = max(math.ceil(total_submissions / per_page), 1) if total_submissions else 1
        if page > total_pages:
            page = total_pages
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
            search_q=search_q
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
            if org_mem and task.organization and getattr(task.organization, 'members_can_edit_tasks', False):
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
                try:
                    new_html = revise_html_with_ai(current_html, instructions, call_ai_model, ai_config)
                except Exception as e:
                    logger.exception('AI 继续修改 HTML 失败')
                    flash(f'AI 修改失败：{str(e)}', 'danger')
                    return redirect(url_for('quickform.edit_task', task_id=task.id))
                static_uploads = _static_uploads_dir()
                unique_filename = str(uuid.uuid4()) + '_revised.html'
                new_filepath = os.path.join(static_uploads, unique_filename)
                with open(new_filepath, 'w', encoding='utf-8') as f:
                    f.write(new_html)
                if task.file_path and os.path.exists(task.file_path):
                    try:
                        os.remove(task.file_path)
                    except Exception:
                        pass
                task.file_path = new_filepath
                task.file_name = 'revised.html'
                task.html_files = json.dumps([{'original_name': 'revised.html', 'saved_name': unique_filename}])
                task.html_ai_edit_remaining = task.html_ai_edit_remaining - 1
                if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                    task.html_approved = 1
                    task.html_approved_by = current_user.id
                    task.html_approved_at = datetime.now()
                else:
                    task.html_approved = 0
                task.html_analysis = None
                db.commit()
                flash(f'已按您的说明完成修改，剩余可修改 {task.html_ai_edit_remaining} 次。', 'success')
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
            # 是否本请求会保存新的 HTML 内容（用于一键生成任务的 3 次修改上限）
            has_new_html = False
            if files_multipart:
                try:
                    flist = [f for f in files_multipart if f and f.filename and (f.filename or '').strip()]
                except Exception:
                    flist = []
            else:
                flist = []
            if flist:
                has_new_html = True
            if html_files_data:
                try:
                    nf = json.loads(html_files_data)
                    if isinstance(nf, list) and len(nf) > 0:
                        has_new_html = True
                except Exception:
                    pass
            if not has_new_html and file_content_base64 and file_name_base64:
                has_new_html = True
            if not has_new_html and file_upload and file_upload.filename and (file_upload.filename or '').strip():
                has_new_html = True
            if has_new_html and getattr(task, 'ai_generated', False) and getattr(task, 'html_ai_edit_remaining', None) is not None and task.html_ai_edit_remaining <= 0:
                flash('一键生成的 HTML 最多可修改 3 次，您已达到上限，无法继续修改。', 'warning')
                return redirect(url_for('quickform.edit_task', task_id=task.id))
            html_was_saved = False  # 本请求是否成功保存了 HTML，用于扣减剩余次数

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
                    html_was_saved = True
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
                    html_was_saved = True
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
                    html_was_saved = True
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
                        html_was_saved = True
            if remove_file:
                if task.file_path and os.path.exists(task.file_path):
                    os.remove(task.file_path)
                task.file_name = None
                task.file_path = None
                task.html_files = None
                task.html_review_note = None
            
            # 可见性/分享类型：公开 或 私有(含组织)；公开仅认证教师或管理员可用
            visibility = request.form.get('visibility')
            if visibility == 'public':
                if current_user.is_admin() or getattr(current_user, 'is_certified', False):
                    task.sharing_type = 'public'
                else:
                    flash('只有通过教师认证的用户才能公开项目到共享区', 'warning')
                    task.sharing_type = 'organization' if task.organization_id else 'private'
            elif visibility == 'private':
                task.sharing_type = 'organization' if task.organization_id else 'private'
            
            # 一键生成任务：每保存一次 HTML 扣减一次剩余修改次数
            if html_was_saved and getattr(task, 'ai_generated', False) and getattr(task, 'html_ai_edit_remaining', None) is not None:
                task.html_ai_edit_remaining = task.html_ai_edit_remaining - 1
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
                f['url'] = get_upload_file_url(f['saved_name'], task.file_path)
        
        task_ai_generated = getattr(task, 'ai_generated', False)
        task_html_ai_edit_remaining = getattr(task, 'html_ai_edit_remaining', None)
        return render_template('edit_task.html', task=task, saved_filename=saved_filename, html_files=html_files,
                               task_ai_generated=task_ai_generated, task_html_ai_edit_remaining=task_html_ai_edit_remaining)
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
        logger.error(f"删除任务失败: {str(e)}", exc_info=True)
        flash(f'删除任务失败: {str(e)}', 'danger')
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
        return jsonify({'success': False, 'message': str(e)}), 500
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
    except Exception as e:
        # 这里捕获 send_email_code 抛出的 RuntimeError 等，给前端返回友好的提示信息
        logger.exception("发送邮箱验证码异常")
        safe_message = str(e) if isinstance(e, RuntimeError) else '服务器内部错误，请稍后重试或联系管理员。'
        return jsonify({
            'success': False,
            'message': safe_message
        }), 500

SUBMIT_RATE_LIMIT_WINDOW = 30   # seconds（教室/公开课场景下适当放宽窗口）
SUBMIT_RATE_LIMIT_THRESHOLD = 200  # 窗口内同一 IP 提交超过此次数则限流（提高以适配课堂集中提交）
SUBMIT_BLACKLIST_DURATION = 300  # seconds

rate_limit_cache = {}

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
        return f'<html><body><p>渲染教程失败：{html.escape(str(e))}</p></body></html>', 500


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

    user = _mcp_authenticate(username, password)
    if not user:
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401

    db = SessionLocal()
    try:
        if not user.can_create_task(SessionLocal, Task):
            task_count = db.query(Task).filter_by(user_id=user.id).count()
            return jsonify({
                'success': False,
                'message': f'已达任务数量上限（当前 {task_count} 个），无法创建新任务'
            }), 403
        task = Task(title=task_name, description=task_intro or None, user_id=user.id)
        db.add(task)
        db.commit()
        db.refresh(task)
        return jsonify({'success': True, 'apiid': task.task_id}), 200
    except Exception as e:
        db.rollback()
        logger.exception('CLI add task failed')
        return jsonify({'success': False, 'message': str(e)}), 500
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

    user = _mcp_authenticate(username, password)
    if not user:
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401

    db = SessionLocal()
    try:
        tasks = db.query(Task).filter_by(user_id=user.id).order_by(Task.created_at.desc()).all()
        out = [{'apiid': t.task_id, 'name': t.title or ''} for t in tasks]
        return jsonify({'success': True, 'tasks': out}), 200
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

    user = _mcp_authenticate(username, password)
    if not user:
        _upload_auth_record_failure(client_key)
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401
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
        return jsonify({'success': False, 'message': str(e)}), 500


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
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response, 200
        
        # POST方法：提交数据
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        now_ts = datetime.utcnow().timestamp()

        ip_info = rate_limit_cache.setdefault(client_ip, {
            'events': deque(),
            'blacklist_until': 0,
            'blocked_tasks': {}
        })

        # 检查黑名单
        if ip_info['blacklist_until'] and now_ts < ip_info['blacklist_until']:
            logger.warning(f"IP {client_ip} 正在黑名单中，拒绝 task_id={task_id} 的提交")
            return _rate_limit_response(task_id, client_ip, now_ts, db)
        
        # 获取提交的数据
        try:
            if request.is_json:
                form_data = request.get_json()
            else:
                form_data = request.form.to_dict()
        except Exception as e:
            logger.error(f"解析请求数据失败: {str(e)}")
            response = jsonify({'error': 'invalid_body', 'message': 'Invalid JSON or form data.', 'detail': str(e)})
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response, 400
        
        # 速率限制处理
        events: Deque = ip_info['events']
        while events and now_ts - events[0] > SUBMIT_RATE_LIMIT_WINDOW:
            events.popleft()
        events.append(now_ts)

        if len(events) > SUBMIT_RATE_LIMIT_THRESHOLD:
            ip_info['blacklist_until'] = now_ts + SUBMIT_BLACKLIST_DURATION
            ip_info['blocked_tasks'][task.id] = now_ts
            logger.warning(
                f"IP {client_ip} 在 {SUBMIT_RATE_LIMIT_WINDOW}s 内提交 {len(events)} 次，已加入黑名单 {SUBMIT_BLACKLIST_DURATION}s"
            )
            return _rate_limit_response(task_id, client_ip, now_ts, db)
        
        # 将数据转换为JSON字符串存储
        try:
            submission = Submission(task_id=task.id, data=json.dumps(form_data, ensure_ascii=False))
            db.add(submission)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"保存提交数据失败: {str(e)}")
            response = jsonify({'error': 'save_failed', 'message': 'Failed to save submission.', 'detail': str(e)})
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response, 500
        
        response = jsonify({'message': 'Submitted successfully.', 'status': 'success'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 200
    except Exception as e:
        logger.error(f"API异常: {str(e)}", exc_info=True)
        response = jsonify({'error': 'internal_error', 'message': 'Internal server error.', 'detail': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 500
    finally:
        db.close()


def _rate_limit_response(task_id, client_ip, ts, db):
    if db:
        task = db.query(Task).filter_by(task_id=task_id).first()
        if task:
            notice = f"IP {client_ip} 在 {SUBMIT_RATE_LIMIT_WINDOW}s 内多次提交，已暂时封禁 {SUBMIT_BLACKLIST_DURATION // 60} 分钟"
            log_entry = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {notice}"
            existing = task.rate_limit_log or ''
            if existing:
                task.rate_limit_log = existing + '\n' + log_entry
            else:
                task.rate_limit_log = log_entry
            try:
                db.commit()
            except Exception as e:
                db.rollback()
                logger.error(f"记录限流日志失败: {str(e)}")
 
    response = jsonify({'error': 'rate_limit', 'message': 'Too many requests. Please try again later.'})
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
        
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    now_ts = datetime.utcnow().timestamp()

    ip_info = rate_limit_cache.setdefault(client_ip, {
        'events': deque(),
        'blacklist_until': 0,
        'blocked_tasks': {},
        'last_all_access': 0  # 记录上次访问 /all 接口的时间
    })
    
    # 限制 /all 接口1秒最多访问1次
    if now_ts - ip_info.get('last_all_access', 0) < 1.0:
        logger.warning(f"IP {client_ip} 访问 /all 接口过快，被限流")
        response = jsonify({'error': 'rate_limit', 'message': 'Too many requests. Limit: at most once per second for this endpoint.'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 429
        
    ip_info['last_all_access'] = now_ts
    
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
        _record_api_get('api_task_all')
        response = jsonify({
            'note': f'Total {total_count} submission(s).',
            'task_id': task.task_id,
            'task_title': task.title,
            'total_submissions': total_count,
            'submissions': data_list
        })
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 200
    except Exception as e:
        logger.error(f"API异常: {str(e)}", exc_info=True)
        response = jsonify({'error': 'internal_error', 'message': 'Internal server error.', 'detail': str(e)})
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
        response = jsonify({'error': 'internal_error', 'message': 'Internal server error.', 'detail': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 500
    finally:
        db.close()


@quickform_bp.route('/api/stats/overview', methods=['GET', 'OPTIONS'])
def public_stats_overview():
    """运营概览统计（JSON）：用户数、学校数（去重）、任务数、提交数。

    安全说明：仅靠「路径不常见」不能防扫描；若需限制访问，请在环境变量中设置 STATS_API_TOKEN，
    请求时携带：查询参数 ?token=...、或请求头 X-Stats-Token、或 Authorization: Bearer ...。
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
        flash(f'导出数据时出错: {str(e)}', 'danger')
        return redirect(url_for('quickform.task_detail', task_id=task_id))
    finally:
        db.close()

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
            logger.error(f"AI配置测试失败: {str(e)}")
            return jsonify({'success': False, 'message': str(e)}), 500

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
        
        if not ai_config or not ai_config.selected_model:
            return render_template('smart_analyze.html', task=task, error="请先在配置页面设置AI模型和API密钥", ai_config=ai_config, now=datetime.now(), model_label=None)
        
        model_label = MODEL_LABELS.get(ai_config.selected_model, ai_config.selected_model)
        
        if ai_config.selected_model not in SUPPORTED_AI_MODELS:
            return render_template('smart_analyze.html', task=task, error=f"{model_label} 暂未集成，敬请期待后续版本", ai_config=ai_config, now=datetime.now(), model_label=model_label)
        elif ai_config.selected_model == 'deepseek' and not ai_config.deepseek_api_key:
            return render_template('smart_analyze.html', task=task, error="请先配置DeepSeek API密钥", ai_config=ai_config, now=datetime.now(), model_label=model_label)
        elif ai_config.selected_model == 'doubao' and not ai_config.doubao_api_key:
            return render_template('smart_analyze.html', task=task, error="请先配置豆包API密钥", ai_config=ai_config, now=datetime.now(), model_label=model_label)
        
        # 如果是提交生成请求，则同步生成并返回同页结果
        if request.method == 'POST':
            # 检查是仅保存模板还是生成报告；report_action：generate 或 polish_and_generate
            action = request.form.get('action', 'generate')  # 'save_template' 或 'generate'
            report_action = request.form.get('report_action', 'generate')
            
            # 获取用户提示词模板（不包含数据部分）
            user_prompt_template = request.form.get('user_prompt_template', '').strip()
            if user_prompt_template:
                # 保存用户模板
                task.user_prompt_template = user_prompt_template
                db.commit()
            
            # 如果只是保存模板，直接返回
            if action == 'save_template':
                flash('提示词模板已保存', 'success')
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
            interface_desc = request.form.get('interface_desc', '').strip()
            file_content_for_prompt = None
            if task.file_path and os.path.exists(task.file_path):
                file_content_for_prompt = read_file_content(task.file_path)
            
            # 若用户在高阶编辑中填写了「完整提示词」，优先使用该内容，否则再根据表单生成
            custom_prompt_from_form = request.form.get('custom_prompt', '').strip()
            if custom_prompt_from_form:
                custom_prompt = custom_prompt_from_form
            else:
                user_prompt_from_form = request.form.get('user_prompt_template', '').strip()
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
                    polished = call_ai_model(polish_prompt, ai_config)
                    if polished and polished.strip():
                        custom_prompt = polished
                except Exception as e:
                    logger.warning(f"润色提示词失败，将使用原提示词: {e}")
            
            # 保存完整提示词（用于兼容旧代码）
            task.custom_prompt = custom_prompt
            db.commit()
            
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
                return render_template('smart_analyze.html', task=task, error=f'生成报告失败: {str(e)}', ai_config=ai_config, now=datetime.now(), model_label=model_label)
        
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
        logger.error(f"下载报告失败: {str(e)}", exc_info=True)
        flash(f'下载报告时出错: {str(e)}', 'danger')
        return redirect(url_for('quickform.dashboard'))
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
        # 权限检查：管理员、任务所有者、组织成员、被共享者可以查看报告状态
        db = SessionLocal()
        try:
            task = db.get(Task, task_id)
            if not task:
                return jsonify({'status': 'error', 'message': '任务不存在'}), 404
            
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
        return jsonify({'status': 'error', 'message': str(e)}), 500

@quickform_bp.route('/admin')
@admin_required
def admin_panel():
    """管理员面板：按当前 tab 仅加载该页数据，避免一次性查全表"""
    db = SessionLocal()
    try:
        today = datetime.now().date()
        today_start = datetime.combine(today, datetime.min.time())
        current_tab = request.args.get('tab', 'users')
        user_per_page = 20
        task_per_page = 20
        html_review_per_page = 20
        cert_review_per_page = 20

        # 顶部统计：用户/管理员/任务始终 count；提交总数仅在「数据报表」tab 查询（避免每次进后台全表扫 submission）
        total_users = db.query(User).count()
        admin_users = db.query(User).filter_by(role='admin').count()
        total_tasks = db.query(Task).count()
        total_submissions = db.query(Submission).count() if current_tab == 'data' else None

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
        org_pending_with_creator = []
        open_source_tasks_with_author = []
        tutorials_json_content = '[]'

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
            all_tasks = (
                db.query(Task)
                .order_by(Task.created_at.desc())
                .offset((task_page - 1) * task_per_page)
                .limit(task_per_page)
                .all()
            )

        elif current_tab == 'html-review':
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

        elif current_tab == 'public-review':
            public_pending_tasks = (
                db.query(Task)
                .filter(Task.sharing_type == 'public', Task.public_approved == 0)
                .order_by(Task.created_at.desc())
                .all()
            )
            author_ids = {t.user_id for t in public_pending_tasks}
            authors_map = {u.id: u for u in db.query(User).filter(User.id.in_(author_ids)).all()} if author_ids else {}
            public_pending_with_author = [{'task': t, 'author': authors_map.get(t.user_id)} for t in public_pending_tasks]

        elif current_tab == 'org-review':
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
            'certified_users': 0,
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
            stats.update(
                normal_users=normal_users,
                new_users_today=new_users_today,
                new_tasks_today=new_tasks_today,
                avg_tasks_per_user=avg_tasks_per_user,
                new_submissions_today=new_submissions_today,
                avg_submissions_per_task=avg_submissions_per_task,
                tasks_with_reports=tasks_with_reports,
                report_generation_rate=report_generation_rate,
                total_organizations=total_organizations,
                total_org_members=total_org_members,
                tasks_in_organizations=tasks_in_organizations,
                certified_users=certified_users,
                public_tasks=public_tasks,
                public_approved_tasks=public_approved_tasks,
                total_task_shares=total_task_shares,
                total_task_likes=total_task_likes,
                ai_generated_tasks=ai_generated_tasks,
                cert_requests_pending=cert_requests_pending,
                total_posts=total_posts,
                total_post_replies=total_post_replies,
            )

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
            org_pending_with_creator=org_pending_with_creator,
            open_source_tasks_with_author=open_source_tasks_with_author,
            tutorials_json_content=tutorials_json_content,
            api_traffic=api_traffic
        )
    finally:
        db.close()


@quickform_bp.route('/admin/public_approve/<int:task_id>', methods=['POST'])
@admin_required
def admin_public_approve(task_id):
    """管理员通过项目公开申请"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.sharing_type != 'public' or task.public_approved != 0:
            flash('任务不存在或无需审核', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='public-review'))
        task.public_approved = 1
        db.commit()
        flash(f'已通过项目「{task.title}」的公开申请，将展示在项目交流页。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='public-review'))


@quickform_bp.route('/admin/public_reject/<int:task_id>', methods=['POST'])
@admin_required
def admin_public_reject(task_id):
    """管理员拒绝项目公开申请"""
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task or task.sharing_type != 'public' or task.public_approved != 0:
            flash('任务不存在或无需审核', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='public-review'))
        task.public_approved = -1
        db.commit()
        flash(f'已拒绝项目「{task.title}」的公开申请。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='public-review'))


@quickform_bp.route('/admin/org_teams_approve/<int:org_id>', methods=['POST'])
@admin_required
def admin_org_teams_approve(org_id):
    """管理员通过组织「入驻团队 / 首页公开」申请"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org or not org.teams_public_requested or org.teams_public_approved != 0:
            flash('组织不存在或无需审核', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='org-review'))
        org.teams_public_approved = 1
        db.commit()
        flash(f'已通过组织「{org.name}」的入驻团队展示申请。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='org-review'))


@quickform_bp.route('/admin/org_teams_reject/<int:org_id>', methods=['POST'])
@admin_required
def admin_org_teams_reject(org_id):
    """管理员拒绝组织「入驻团队」申请"""
    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        if not org or not org.teams_public_requested or org.teams_public_approved != 0:
            flash('组织不存在或无需审核', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='org-review'))
        org.teams_public_approved = -1
        db.commit()
        flash(f'已拒绝组织「{org.name}」的入驻团队展示申请。创建者可改为「内部交流」后重新申请。', 'success')
    finally:
        db.close()
    return redirect(url_for('quickform.admin_panel', tab='org-review'))


@quickform_bp.route('/admin/public_batch_approve', methods=['POST'])
@admin_required
def admin_public_batch_approve():
    """管理员批量通过项目公开申请"""
    task_ids = request.form.getlist('task_ids')
    if not task_ids:
        flash('请先勾选要通过的项目', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='public-review'))
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
    return redirect(url_for('quickform.admin_panel', tab='public-review'))


@quickform_bp.route('/admin/public_batch_reject', methods=['POST'])
@admin_required
def admin_public_batch_reject():
    """管理员批量拒绝项目公开申请"""
    task_ids = request.form.getlist('task_ids')
    if not task_ids:
        flash('请先勾选要拒绝的项目', 'warning')
        return redirect(url_for('quickform.admin_panel', tab='public-review'))
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
    return redirect(url_for('quickform.admin_panel', tab='public-review'))


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
        logger.error(f"重置密码失败: {str(e)}")
        return jsonify({'success': False, 'message': f'重置失败: {str(e)}'}), 500
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
            logger.error(f"删除用户失败: {str(e)}", exc_info=True)
            flash(f'删除用户失败: {str(e)}', 'danger')
            
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
        logger.error(f"导出用户数据失败: {str(e)}", exc_info=True)
        flash(f'导出数据时出错: {str(e)}', 'danger')
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
        return jsonify({'success': False, 'message': str(e)}), 500
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
        logger.error(f"获取统计数据失败: {str(e)}", exc_info=True)
        flash(f'获取统计数据失败: {str(e)}', 'danger')
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
        return jsonify({'success': False, 'message': str(e)}), 500
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
            return redirect(url_for('quickform.admin_panel', tab='html-review'))

        tasks = db.query(Task).filter(Task.id.in_(task_ids)).all()
        if not tasks:
            flash('未找到所选任务', 'warning')
            return redirect(url_for('quickform.admin_panel', tab='html-review'))

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
        logger.error(f"批量HTML审核失败: {str(e)}")
        flash(f'批量审核失败：{str(e)}', 'danger')
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

            cert_request.status = 1
            cert_request.reviewed_at = datetime.now()
            cert_request.reviewed_by = current_user.id
            cert_request.review_note = note

            user.is_certified = True
            user.certified_at = datetime.now()
            user.certification_note = note
            if user.task_limit != -1:
                user.task_limit = -1

            # 自动通过该用户所有待审核的HTML任务
            pending_tasks = db.query(Task).filter(Task.user_id == user.id, Task.html_approved != 1).all()
            for task in pending_tasks:
                task.html_approved = 1
                task.html_approved_by = current_user.id
                task.html_approved_at = datetime.now()
                task.html_review_note = None

            db.commit()
            flash(f'已通过 {user.username} 的认证申请，任务上限已调整为无限制。', 'success')
        elif action == 'reject':
            if cert_request.status == -1:
                flash('该认证申请已被拒绝', 'info')
                return redirect(url_for('quickform.admin_panel', tab='cert-review'))

            cert_request.status = -1
            cert_request.reviewed_at = datetime.now()
            cert_request.reviewed_by = current_user.id
            cert_request.review_note = note
            db.commit()
            flash('已拒绝该认证申请。', 'warning')
        else:
            flash('无效的操作类型', 'danger')
    except Exception as e:
        db.rollback()
        logger.error(f"认证审核处理失败: {str(e)}")
        flash(f'处理失败：{str(e)}', 'danger')
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
        logger.error(f"批量通过教师认证申请失败: {str(e)}")
        flash(f'批量处理失败：{str(e)}', 'danger')
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
            return redirect(url_for('quickform.admin_panel', tab='html-review'))
        
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
            return redirect(url_for('quickform.admin_panel', tab='html-review'))
        
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
                return redirect(url_for('quickform.admin_panel', tab='html-review'))
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
    
    return redirect(url_for('quickform.admin_panel', tab='html-review'))

@quickform_bp.route('/task/<int:task_id>/submission/remove', methods=['GET'])
@login_required
def remove_submission(task_id):
    """删除单条提交数据（支持DELETE与GET降级）"""
    db = SessionLocal()
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    submission_id = request.args.get('submission_id', type=int)

    def make_response(payload, status_code=200):
        resp = jsonify(payload)
        resp.status_code = status_code
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    logger.info(
        f"[remove_submission] GET user={getattr(current_user, 'id', None)} "
        f"task={task_id} submission={submission_id} ip={client_ip}"
    )
    try:
        task = db.get(Task, task_id)
        if not task:
            return make_response({'success': False, 'message': '任务不存在'}, 404)
        share_rec = db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first()
        org_mem = db.query(OrganizationMember).filter_by(
            organization_id=task.organization_id, user_id=current_user.id
        ).first() if task.organization_id else None
        can_edit = (
            current_user.is_admin() or task.user_id == current_user.id or
            (org_mem and task.organization and getattr(task.organization, 'members_can_edit_tasks', False)) or
            (share_rec and share_rec.can_edit)
        )
        if not can_edit:
            logger.warning(
                f"[remove_submission] forbidden user={getattr(current_user, 'id', None)} task={task_id}"
            )
            return make_response({'success': False, 'message': '无权删除此任务的数据（仅拥有编辑权限时可删除）'}, 403)
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
        logger.info(
            f"[remove_submission] success user={getattr(current_user, 'id', None)} task={task_id} submission={submission_id}"
        )
        return make_response({'success': True, 'message': '删除成功'})
    except Exception as e:
        db.rollback()
        logger.error(
            f"[remove_submission] error task={task_id} submission={submission_id} err={str(e)}",
            exc_info=True
        )
        return make_response({'success': False, 'message': f'删除失败: {str(e)}'}, 500)
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/submissions/clear', methods=['GET'])
@login_required
def clear_all_submissions(task_id):
    """删除任务的所有提交数据（支持DELETE与GET降级）"""
    db = SessionLocal()
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    def make_response(payload, status_code=200):
        resp = jsonify(payload)
        resp.status_code = status_code
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    logger.info(
        f"[clear_all_submissions] GET user={getattr(current_user, 'id', None)} task={task_id} ip={client_ip}"
    )
    try:
        task = db.get(Task, task_id)
        if not task:
            return make_response({'success': False, 'message': '任务不存在'}, 404)
        share_rec = db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first()
        org_mem = db.query(OrganizationMember).filter_by(
            organization_id=task.organization_id, user_id=current_user.id
        ).first() if task.organization_id else None
        can_edit = (
            current_user.is_admin() or task.user_id == current_user.id or
            (org_mem and task.organization and getattr(task.organization, 'members_can_edit_tasks', False)) or
            (share_rec and share_rec.can_edit)
        )
        if not can_edit:
            logger.warning(
                f"[clear_all_submissions] forbidden user={getattr(current_user, 'id', None)} task={task_id}"
            )
            return make_response({'success': False, 'message': '无权删除此任务的数据（仅拥有编辑权限时可删除）'}, 403)
        
        submissions = db.query(Submission).filter_by(task_id=task_id).all()
        count = len(submissions)
        logger.info(
            f"[clear_all_submissions] deleting count={count} user={getattr(current_user, 'id', None)} task={task_id}"
        )
        for submission in submissions:
            db.delete(submission)
        
        db.commit()
        logger.info(
            f"[clear_all_submissions] success user={getattr(current_user, 'id', None)} task={task_id} deleted={count}"
        )
        return make_response({'success': True, 'message': f'成功删除 {count} 条数据'})
    except Exception as e:
        db.rollback()
        logger.error(
            f"[clear_all_submissions] error task={task_id} err={str(e)}",
            exc_info=True
        )
        return make_response({'success': False, 'message': f'删除失败: {str(e)}'}, 500)
    finally:
        db.close()


@quickform_bp.route('/task/<int:task_id>/submissions/clear_by_range', methods=['GET'])
@login_required
def clear_submissions_by_date_range(task_id):
    """按提交日期期间删除数据。参数：date_start（YYYY-MM-DD）、date_end（YYYY-MM-DD），均为必填。"""
    from datetime import datetime as dt
    db = SessionLocal()
    date_start_s = request.args.get('date_start', '').strip()
    date_end_s = request.args.get('date_end', '').strip()

    def make_response(payload, status_code=200):
        resp = jsonify(payload)
        resp.status_code = status_code
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    try:
        task = db.get(Task, task_id)
        if not task:
            return make_response({'success': False, 'message': '任务不存在'}, 404)
        share_rec = db.query(TaskShare).filter_by(task_id=task.id, user_id=current_user.id).first()
        org_mem = db.query(OrganizationMember).filter_by(
            organization_id=task.organization_id, user_id=current_user.id
        ).first() if task.organization_id else None
        can_edit = (
            current_user.is_admin() or task.user_id == current_user.id or
            (org_mem and task.organization and getattr(task.organization, 'members_can_edit_tasks', False)) or
            (share_rec and share_rec.can_edit)
        )
        if not can_edit:
            return make_response({'success': False, 'message': '无权删除此任务的数据（仅拥有编辑权限时可删除）'}, 403)
        if not date_start_s or not date_end_s:
            return make_response({'success': False, 'message': '请填写开始日期和结束日期'}, 400)
        try:
            start_date = dt.strptime(date_start_s, '%Y-%m-%d').date()
            end_date = dt.strptime(date_end_s, '%Y-%m-%d').date()
        except ValueError:
            return make_response({'success': False, 'message': '日期格式应为 YYYY-MM-DD'}, 400)
        if start_date > end_date:
            return make_response({'success': False, 'message': '开始日期不能晚于结束日期'}, 400)

        # 该任务下、提交日期在 [start_date, end_date] 之间的记录（按 submitted_at 的日期比较）
        submissions = (
            db.query(Submission)
            .filter_by(task_id=task_id)
            .filter(Submission.submitted_at.isnot(None))
            .all()
        )
        to_delete = [s for s in submissions if s.submitted_at and start_date <= s.submitted_at.date() <= end_date]
        count = len(to_delete)
        for s in to_delete:
            db.delete(s)
        db.commit()
        return make_response({'success': True, 'message': f'已删除该期间内 {count} 条数据'})
    except Exception as e:
        db.rollback()
        logger.exception('clear_submissions_by_date_range error')
        return make_response({'success': False, 'message': f'删除失败: {str(e)}'}, 500)
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
    
    logger.info("QuickForm Blueprint 初始化完成")


# ==================== 组织/团队管理路由 ====================

@quickform_bp.route('/teams')
def teams_list():
    """入驻团队：展示全部团队列表，支持搜索与分页；无需登录可访问；点击加入需填写组织代码（需登录）"""
    db = SessionLocal()
    try:
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
            })
        return render_template('teams_list.html',
                             team_rows=team_rows,
                             q=q,
                             page=page,
                             per_page=per_page,
                             total_count=total_count,
                             total_pages=total_pages)
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
        org_tasks = db.query(Task).filter_by(organization_id=org_id).all()
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


