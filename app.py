"""QuickForm 独立应用入口（配合 Nginx 反向代理时，Flask 仅提供 HTTP 内网服务，SSL 由 Nginx 终结）"""
import os
import time
import threading
from datetime import timedelta
from flask import Flask, redirect, url_for, request
from flask_login import LoginManager, current_user
from flask_bcrypt import Bcrypt
from dotenv import load_dotenv
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# ---------- 404 限流（防扫描） ----------
# 同一 IP 在时间窗口内 404 次数超过阈值则短时拒绝，减轻扫描压力。
# /static/ 与 /uploads/ 不参与限流封禁，保证静态页与教师上传页面可被直接访问；提交仍受 blueprint 内限流约束。
RATE_LIMIT_WINDOW = 60          # 秒
RATE_LIMIT_404_MAX = 30         # 窗口内 404 超过此次数则限流
RATE_LIMIT_BAN_SECONDS = 120    # 触发限流后禁止该 IP 的时长（秒）
_404_count = {}                 # ip -> (count, window_start)
_404_lock = threading.Lock()
_ban_until = {}                 # ip -> unix timestamp 解禁时间


def _get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr or '') or 'unknown'


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
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB（教师认证上传等）；任务内 HTML 单文件限制 4MB 在业务层校验
# 由 Nginx 做 HTTPS 时，仍生成 https 链接（依赖 ProxyFix 传递 X-Forwarded-Proto）
app.config['PREFERRED_URL_SCHEME'] = 'https'

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

# 邮件发送配置（用于邮箱验证码）
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.163.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')  # 你的163邮箱，例如 xxx@163.com
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')  # 163邮箱的SMTP授权码（不要写死在代码里）
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

# 国际化支持
from core.i18n import translate, get_locale, get_locale_name
@app.context_processor
def inject_locale():
    """注入语言环境到模板"""
    return dict(get_locale=get_locale, translate=translate, get_locale_name=get_locale_name)

# 初始化扩展
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'quickform.login'
login_manager.session_protection = 'strong'  # User-Agent/IP 变化时要求重新登录，减少共用电脑串号

bcrypt = Bcrypt(app)

# 导入模型
from core.models import Base, User, Task, Submission, AIConfig

# 数据库配置 - 优先检查MySQL配置，如果配置了MySQL就使用MySQL
MYSQL_HOST = os.getenv('MYSQL_HOST', '')
MYSQL_USER = os.getenv('MYSQL_USER', '')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD', '')
MYSQL_DATABASE = os.getenv('MYSQL_DATABASE', 'quickform')

# 如果环境变量中明确指定了DATABASE_TYPE，使用指定的类型
# 否则，如果MySQL配置完整，优先使用MySQL；否则使用SQLite
if os.getenv('DATABASE_TYPE'):
    DATABASE_TYPE = os.getenv('DATABASE_TYPE', 'sqlite')
elif MYSQL_HOST and MYSQL_USER and MYSQL_PASSWORD:
    DATABASE_TYPE = 'mysql'
    logger.info("检测到 MySQL 配置，将使用 MySQL 数据库")
else:
    DATABASE_TYPE = 'sqlite'
    logger.info("未检测到 MySQL 配置，将使用 SQLite 数据库")

# 导入并注册QuickForm Blueprint
from core import blueprint as quickform_blueprint
quickform_bp = quickform_blueprint.quickform_bp
quickform_blueprint.init_quickform(app, login_manager, database_type=DATABASE_TYPE)

# 注册Blueprint
app.register_blueprint(quickform_bp)

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
    """兜底：未捕获异常统一记录并返回 500，避免进程崩溃或暴露堆栈；HTTPException(404/400等)不在此处理"""
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        return error.get_response()
    logger.exception("未捕获异常: %s", error)
    return 'Internal Server Error', 500

@app.errorhandler(413)
def request_entity_too_large(error):
    from flask import flash, request
    app.logger.warning("413错误 - 请求实体过大")
    # 若来自认证申请页，则回到认证页并给出针对性提示
    referrer = (request.referrer or '') if request.referrer else ''
    if 'certification' in referrer or (request.path and 'certification' in request.path):
        flash('认证材料文件过大，请压缩或更换为更小的文件后重试（单文件最大 10MB）。', 'danger')
        return redirect(url_for('quickform.certification_request'))
    flash('文件大小超过服务器限制（最大10MB），请压缩后重试。', 'danger')
    return redirect(url_for('quickform.dashboard'))


if __name__ == '__main__':
    # 供 Nginx 反向代理：仅 HTTP，监听本机；端口由环境变量指定，默认 5000
    host = os.getenv('FLASK_HOST', '127.0.0.1')
    port = int(os.getenv('FLASK_PORT', '5000'))
    logger.info("Flask 启动: %s:%s（由 Nginx 转发时请使用此方式）", host, port)
    app.run(
        host=host,
        port=port,
        debug=os.getenv('FLASK_DEBUG', 'False').lower() == 'true',
        use_reloader=False,
        threaded=True,
    )