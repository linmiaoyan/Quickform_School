"""QuickForm 独立应用入口（配合 Nginx 反向代理时，Flask 仅提供 HTTP 内网服务，SSL 由 Nginx 终结）"""
import os
import time
import threading
from datetime import timedelta
from flask import Flask, redirect, url_for, request, make_response
from flask_login import LoginManager, current_user
from flask_bcrypt import Bcrypt
from dotenv import load_dotenv
import logging
from logging.handlers import RotatingFileHandler
import re

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def setup_file_logging():
    """将日志同时输出到控制台和文件（按大小轮转）"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(base_dir, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    log_level_name = (os.getenv('APP_LOG_LEVEL', 'INFO') or 'INFO').upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    max_bytes = int(os.getenv('APP_LOG_MAX_BYTES', str(10 * 1024 * 1024)))
    backup_count = int(os.getenv('APP_LOG_BACKUP_COUNT', '10'))

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    app_log_path = os.path.join(logs_dir, 'app.log')
    error_log_path = os.path.join(logs_dir, 'error.log')

    def _has_same_file_handler(target_path):
        target_norm = os.path.normcase(os.path.abspath(target_path))
        for h in root_logger.handlers:
            if isinstance(h, RotatingFileHandler):
                h_path = getattr(h, 'baseFilename', '')
                if os.path.normcase(os.path.abspath(h_path)) == target_norm:
                    return True
        return False

    if not _has_same_file_handler(app_log_path):
        app_file_handler = RotatingFileHandler(
            app_log_path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8'
        )
        app_file_handler.setLevel(log_level)
        app_file_handler.setFormatter(fmt)
        root_logger.addHandler(app_file_handler)

    if not _has_same_file_handler(error_log_path):
        error_file_handler = RotatingFileHandler(
            error_log_path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8'
        )
        error_file_handler.setLevel(logging.ERROR)
        error_file_handler.setFormatter(fmt)
        root_logger.addHandler(error_file_handler)

# 加载环境变量
load_dotenv()
setup_file_logging()

# ---------- 404 限流（防扫描） ----------
# 同一 IP 在时间窗口内 404 次数超过阈值则短时拒绝，减轻扫描压力。
# /static/ 与 /uploads/ 不参与限流封禁，保证静态页与教师上传页面可被直接访问；提交仍受 blueprint 内限流约束。
RATE_LIMIT_WINDOW = 60          # 秒
RATE_LIMIT_404_MAX = 30         # 窗口内 404 超过此次数则限流
RATE_LIMIT_BAN_SECONDS = 120    # 触发限流后禁止该 IP 的时长（秒）
_404_count = {}                 # ip -> (count, window_start)
_404_lock = threading.Lock()
_ban_until = {}                 # ip -> unix timestamp 解禁时间
_HASHED_STATIC_RE = re.compile(r'\.[0-9a-f]{8,}\.(css|js|png|jpg|jpeg|gif|webp|svg|woff|woff2)$', re.IGNORECASE)


from core.client_ip import get_request_client_ip


def _get_client_ip():
    return get_request_client_ip(request)


def _clean_old_entries():
    """清理过期的 404 记录和封禁信息

    注意：调用方必须先获取 _404_lock 再调用本函数，
    本函数内部不再重复加锁，以避免死锁。
    """
    now = time.time()
    for ip in list(_404_count.keys()):
        _, start = _404_count[ip]
        if now - start > RATE_LIMIT_WINDOW:
            del _404_count[ip]
    for ip in list(_ban_until.keys()):
        if _ban_until[ip] < now:
            del _ban_until[ip]


# 创建Flask应用
app = Flask(__name__)
_secret_key = (os.getenv('SECRET_KEY') or '').strip()
if not _secret_key or _secret_key == 'your_secret_key_here':
    raise RuntimeError('SECRET_KEY 未配置或仍为弱默认值，请在环境变量中设置强随机 SECRET_KEY 后再启动。')
app.secret_key = _secret_key
# 站点版本号（浏览器标题/页脚展示；可用环境变量覆盖，例如 1.0.0 或 2026.05.02）
app.config['APP_VERSION'] = (os.getenv('APP_VERSION') or '1.0.0').strip() or '1.0.0'
# 整包请求上限（与 MAX_REQUEST_BODY_MB 一致）；API 多模态单文件见 API_MAX_FILE_SIZE_MB
from core.api_submit import api_max_request_body_bytes

app.config['MAX_CONTENT_LENGTH'] = api_max_request_body_bytes()
app.config['JSON_AS_ASCII'] = False  # API 错误 message 直接输出中文，避免 \uXXXX 展示困扰
# 站点对外 scheme 默认跟随请求/反代头；如需固定请用 PUBLIC_BASE_URL 显式配置
app.config['PREFERRED_URL_SCHEME'] = os.getenv('PREFERRED_URL_SCHEME', 'http').strip().lower() or 'http'
# 对外站点根 URL（无末尾斜杠），用于一键生成嵌入 API 地址等；不配置时从请求头推断，见 blueprint._public_site_base_url
_app_public_base = (os.getenv('PUBLIC_BASE_URL') or os.getenv('QUICKFORM_PUBLIC_BASE_URL') or '').strip().rstrip('/')
app.config['PUBLIC_BASE_URL'] = _app_public_base

# ---------- Session/Cookie 配置（避免未登录却显示他人账号的 cookie 串号问题）----------
# 使用独立 cookie 名，避免同域名下其他应用共用/覆盖 session
app.config['SESSION_COOKIE_NAME'] = 'quickform_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'   # 防止跨站请求携带 cookie，减少串号与 CSRF
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', 'true').lower() == 'true'  # HTTPS 时建议为 True
# Flask-Login「记住我」cookie 独立命名与安全属性
app.config['REMEMBER_COOKIE_NAME'] = 'quickform_remember'
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = os.getenv('REMEMBER_COOKIE_SECURE', 'true').lower() == 'true'
# 不设置 REMEMBER_COOKIE_DOMAIN，保持仅当前主机，避免子域共用导致看到别人账号
# 登录态持久化：会话 cookie 有效期，重启服务后用户仍保持登录（需在登录时设置 session.permanent = True）
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)
# Flask-Login「记住我」cookie 默认 365 天，如需可设置 REMEMBER_COOKIE_DURATION
# 会话保护：strong 会在 User-Agent/IP 变化时要求重新登录，降低共用电脑时的误用

# 一键内测 / 硅基流动：用户未在个人中心配置时，使用此处提供的 API（环境变量 CHAT_SERVER_API_TOKEN）
app.config['CHAT_SERVER_API_TOKEN'] = os.getenv('CHAT_SERVER_API_TOKEN', '')
# 硅基流动模型 ID（OpenAI 兼容接口的 model 字段）；轻量场景见 CHAT_SERVER_MODEL_LIGHT
_default_sf = (os.getenv('CHAT_SERVER_MODEL') or 'Pro/deepseek-ai/DeepSeek-V3.2').strip()
app.config['CHAT_SERVER_MODEL'] = _default_sf or 'Pro/deepseek-ai/DeepSeek-V3.2'
_light_sf = (os.getenv('CHAT_SERVER_MODEL_LIGHT') or 'deepseek-ai/DeepSeek-V3.2').strip()
app.config['CHAT_SERVER_MODEL_LIGHT'] = _light_sf or 'deepseek-ai/DeepSeek-V3.2'

# 邮件发送配置（用于邮箱验证码）
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.163.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')  # 你的163邮箱，例如 xxx@163.com
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')  # 163邮箱的SMTP授权码（不要写死在代码里）
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

# 国际化支持
from core.i18n import translate, get_locale, get_locale_name
from core.system_config import SystemConfig, load_system_config


@app.context_processor
def inject_locale():
    """注入语言环境到模板"""
    return dict(get_locale=get_locale, translate=translate, get_locale_name=get_locale_name)


@app.context_processor
def inject_site_branding():
    """全站：系统配置（名称等）与版本号，供标题栏与页脚使用。"""
    try:
        cfg = load_system_config()
    except Exception:
        cfg = SystemConfig()
    return {
        'system_config': cfg,
        'syscfg': cfg,
        'app_version': (app.config.get('APP_VERSION') or '1.0.0'),
    }

@app.context_processor
def inject_user_capabilities():
    """登录用户能力（如是否可新建任务）与 QF 小公告未读数。"""
    from flask_login import current_user
    from core.multimodal_hints import MULTIMODAL_REFERENCE_PROMPT

    can_create = True
    can_import = True
    qf_notice_unread = 0
    try:
        if current_user.is_authenticated:
            if current_user.is_admin():
                can_create = True
                can_import = True
            elif getattr(current_user, 'qflink_disabled', False):
                can_create = False
                can_import = False
            elif getattr(current_user, 'qflink_uid', None):
                can_create = False
                can_import = True
            else:
                can_create = True
                can_import = True
            try:
                from core.db import SessionLocal
                from core.qf_notice import count_unread_notices
                db = SessionLocal()
                try:
                    qf_notice_unread = count_unread_notices(db, current_user.id)
                finally:
                    db.close()
            except Exception:
                qf_notice_unread = 0
    except Exception:
        can_create = True
        can_import = True
    return {
        'user_can_create_task': can_create,
        'user_can_import_task': can_import,
        'qf_notice_unread': qf_notice_unread,
        'multimodal_reference_prompt': MULTIMODAL_REFERENCE_PROMPT,
    }


# 初始化扩展
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'quickform.login'
login_manager.session_protection = 'strong'  # User-Agent/IP 变化时要求重新登录，减少共用电脑串号

bcrypt = Bcrypt(app)

# 导入模型
from core.models import Base, User, Task, Submission, AIConfig

# 数据库配置（校园版：PostgreSQL 起步）
# DATABASE_TYPE 仍保留参数位以兼容 init_quickform_async 的签名，但 blueprint 内会按 postgresql 初始化。
DATABASE_TYPE = os.getenv('DATABASE_TYPE', 'postgres')

# 导入并注册QuickForm Blueprint
from core import blueprint as quickform_blueprint
quickform_bp = quickform_blueprint.quickform_bp

# 先注册 Blueprint，让维护页 gate 生效；再后台初始化数据库迁移/管理员等，避免启动期阻塞
app.register_blueprint(quickform_bp)
quickform_blueprint.init_quickform_async(app, login_manager, database_type=DATABASE_TYPE)

# 反向代理（Nginx）时信任 X-Forwarded-*，使 request.is_secure 与 url_for 正确
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
except ImportError:
    pass

# User loader
@login_manager.user_loader
def load_user(user_id):
    # 始终使用 blueprint 当前会话工厂，避免数据库重初始化后仍引用旧连接池
    db = quickform_blueprint.SessionLocal()
    try:
        return db.get(User, int(user_id))
    finally:
        db.close()

# 首页重定向
@app.route('/')
def index():
    """根路径：根据登录状态重定向到不同页面，并输出调试日志"""
    try:
        logger.info("处理根路径 / ，当前登录状态: %s", current_user.is_authenticated)
    except Exception:
        pass

    if current_user.is_authenticated:
        logger.info("用户已登录，重定向到 quickform.dashboard")
        return redirect(url_for('quickform.dashboard'))

    logger.info("用户未登录，重定向到 quickform.index")
    return redirect(url_for('quickform.index'))


@app.route('/ping')
def ping():
    """简单健康检查路由，用于确认 Flask 能返回内容"""
    return 'pong', 200

# ---------- 404 限流：请求前检查是否被禁（/static/、/uploads/ 豁免，访问不限） ----------
@app.before_request
def before_request_rate_limit():
    path = (request.path or '').lstrip('/')
    if path.startswith('static/') or path.startswith('uploads/'):
        return None
    ip = _get_client_ip()
    now = time.time()
    try:
        pass
    except Exception:
        pass

    with _404_lock:
        _clean_old_entries()
        if ip in _ban_until and _ban_until[ip] > now:
            logger.warning("IP %s 命中 404 限流，返回 429", ip)
            return 'Too Many Requests', 429


@app.before_request
def handle_api_cors_preflight():
    """统一处理 /api/* 的 CORS 预检，减少业务路由开销。"""
    path = (request.path or '').lstrip('/')
    if request.method == 'OPTIONS' and path.startswith('api/'):
        resp = make_response('', 204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        req_headers = request.headers.get('Access-Control-Request-Headers', '')
        resp.headers['Access-Control-Allow-Headers'] = req_headers or 'Content-Type, Authorization, X-Requested-With'
        resp.headers['Access-Control-Max-Age'] = os.getenv('CORS_MAX_AGE', '600')
        resp.headers['Cache-Control'] = 'public, max-age=600'
        return resp


# ---------- 404 限流：请求后统计 404（/static/、/uploads/ 不统计） ----------
@app.after_request
def after_request_404_track(response):
    path = (request.path or '').lstrip('/')
    if path.startswith('static/') or path.startswith('uploads/'):
        return response
    if response.status_code != 404:
        return response
    ip = _get_client_ip()
    now = time.time()
    with _404_lock:
        if ip not in _404_count:
            _404_count[ip] = (0, now)
        cnt, start = _404_count[ip]
        if now - start > RATE_LIMIT_WINDOW:
            _404_count[ip] = (1, now)
            cnt = 1
        else:
            cnt += 1
            _404_count[ip] = (cnt, start)
        if cnt >= RATE_LIMIT_404_MAX:
            _ban_until[ip] = now + RATE_LIMIT_BAN_SECONDS
            logger.warning("404限流: IP %s 在 %ds 内 404 达 %d 次，已临时限制 %ds", ip, RATE_LIMIT_WINDOW, cnt, RATE_LIMIT_BAN_SECONDS)
    return response


@app.after_request
def apply_cache_and_cors_headers(response):
    """统一补充静态资源缓存头与 API CORS 头。"""
    path = (request.path or '').lstrip('/')

    # 1) 静态资源缓存优化：减少重复 304 往返
    if path.startswith('static/') and request.method == 'GET' and response.status_code in (200, 304):
        cache_seconds = int(os.getenv('STATIC_CACHE_MAX_AGE', '86400'))  # 默认 1 天
        if _HASHED_STATIC_RE.search(path):
            cache_seconds = int(os.getenv('STATIC_HASHED_CACHE_MAX_AGE', str(31536000)))  # 默认 1 年
            response.headers['Cache-Control'] = f'public, max-age={cache_seconds}, immutable'
        else:
            response.headers['Cache-Control'] = f'public, max-age={cache_seconds}'

    # 2) API CORS 响应头：包含预检缓存时间，减少 OPTIONS 次数
    if path.startswith('api/'):
        response.headers.setdefault('Access-Control-Allow-Origin', '*')
        response.headers.setdefault('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        response.headers.setdefault('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With')
        response.headers.setdefault('Access-Control-Max-Age', os.getenv('CORS_MAX_AGE', '600'))

    return response


# 配置日志过滤器
class SecurityScanFilter(logging.Filter):
    def filter(self, record):
        if hasattr(record, 'getMessage'):
            msg = record.getMessage()
            if any(x in msg for x in ['RTSP/1.0', 'Bad request version', 'Bad HTTP/0.9']):
                return False
        return True

werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.addFilter(SecurityScanFilter())

# ---------- 错误处理（保证服务通畅，不因单次异常挂掉） ----------
@app.errorhandler(400)
def bad_request(error):
    return 'Bad Request', 400

@app.errorhandler(404)
def not_found(error):
    return 'Not Found', 404

@app.errorhandler(429)
def too_many_requests(error):
    return 'Too Many Requests', 429

@app.errorhandler(500)
def internal_error(error):
    logger.exception("500 Internal Server Error: %s", error)
    return 'Internal Server Error', 500


@app.errorhandler(Exception)
def handle_uncaught_exception(error):
    """兜底：未捕获异常统一记录并返回 500。

    仅影响**当前请求**的响应（如返回纯文本 Internal Server Error），**不会**因此退出 Flask/Waitress 进程。
    第三方 AI 欠费/限流等应在业务层捕获并提示用户；若仍漏到这里，同样只记日志并返回 500。
    HTTPException（404/400 等）交给 Werkzeug 默认响应，不在此吞掉。
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        return error.get_response()
    logger.exception("未捕获异常: %s", error)
    return 'Internal Server Error', 500

@app.errorhandler(413)
def request_entity_too_large(error):
    from flask import flash, request
    from core.api_submit import api_max_request_body_bytes, submit_api_json_response

    app.logger.warning("413错误 - 请求实体过大")
    path = (request.path or '')
    if path.startswith('/api/') or '/api/' in path:
        mb = max(1, api_max_request_body_bytes() // (1024 * 1024))
        return submit_api_json_response(
            'request_entity_too_large',
            f'请求总大小超过服务器限制（约 {mb}MB）。请勿把大文件写入 JSON；请用表单 multipart 的 file 字段上传附件，或压缩后分批提交。',
            413,
        )
    flash(
        f'文件大小超过服务器限制（约 {api_max_request_body_bytes() // (1024 * 1024)}MB），请压缩后重试。',
        'danger',
    )
    return redirect(url_for('quickform.dashboard'))


if __name__ == '__main__':
    # 供直连/反向代理：仅 HTTP；端口由环境变量指定，默认 80
    host = os.getenv('FLASK_HOST', '127.0.0.1')
    port = int(os.getenv('FLASK_PORT', '80'))
    logger.info("Flask 启动: %s:%s（由 Nginx 转发时请使用此方式）", host, port)
    app.run(
        host=host,
        port=port,
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true',
        use_reloader=False,
        threaded=True,
    )