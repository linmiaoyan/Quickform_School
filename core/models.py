"""数据库模型定义和迁移"""
import os
from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, ForeignKey, Boolean, inspect, text, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from flask_login import UserMixin
from datetime import datetime
import uuid
import secrets
import string
import logging

logger = logging.getLogger(__name__)


def _generate_task_id():
    """生成10位API随机码：小写字母和数字，无下划线，最后两位为字母"""
    chars_any = string.ascii_lowercase + string.digits  # a-z, 0-9
    chars_letter = string.ascii_lowercase  # a-z
    part1 = ''.join(secrets.choice(chars_any) for _ in range(8))
    part2 = ''.join(secrets.choice(chars_letter) for _ in range(2))
    return part1 + part2

Base = declarative_base()


class User(UserMixin, Base):
    __tablename__ = 'user'
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    email = Column(String(100), unique=True, nullable=False)  # 注册可不填时存占位邮箱 {username}@noreply.local；创建第二个任务时需绑定并验证
    password = Column(String(200), nullable=False)
    school = Column(String(200))
    # 学校所在省份（仅用于统计/地区编码；支持管理员手动覆盖，避免自动解析错误）
    # - school_province_source='auto'：由解析逻辑从 school 文本推断
    # - school_province_source='admin'：管理员手动填写覆盖
    school_province = Column(String(50))
    school_province_source = Column(String(20), default='auto')
    phone = Column(String(20))
    role = Column(String(20), default='user')
    task_limit = Column(Integer, default=3)  # 任务创建上限，-1表示无限制
    email_verified = Column(Boolean, default=False)  # 创建第二个任务前需验证邮箱；启动时 migrate_database 会自动为旧表添加该列
    is_certified = Column(Boolean, default=False)
    certified_at = Column(DateTime)
    certification_note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    tasks = relationship('Task', back_populates='author', foreign_keys='Task.user_id', cascade='all, delete-orphan')
    ai_config = relationship('AIConfig', back_populates='user', uselist=False, cascade='all, delete-orphan')
    certification_requests = relationship(
        'CertificationRequest',
        foreign_keys='CertificationRequest.user_id',
        back_populates='user',
        cascade='all, delete-orphan'
    )
    
    def is_admin(self):
        """检查用户是否为管理员"""
        return self.role == 'admin'
    
    def can_create_task(self, SessionLocal, Task):
        """检查用户是否可以创建新任务"""
        db = SessionLocal()
        try:
            # 重新获取最新的用户数据，避免使用登录时旧的 task_limit
            refreshed_user = db.get(User, self.id)
            task_limit = refreshed_user.task_limit if refreshed_user else self.task_limit
            
            if self.is_admin():
                return True
            if task_limit == -1:
                return True
            
            task_count = db.query(Task).filter_by(user_id=self.id).count()
            return task_count < task_limit
        finally:
            db.close()


class Task(Base):
    __tablename__ = 'task'
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    user_id = Column(Integer, ForeignKey('user.id'))
    author = relationship('User', back_populates='tasks', foreign_keys=[user_id])
    submission = relationship('Submission', back_populates='task', cascade='all, delete-orphan')
    file_name = Column(String(200))
    file_path = Column(String(500))
    task_id = Column(String(50), unique=True, default=_generate_task_id)
    analysis_report = Column(Text)
    report_file_path = Column(String(500))
    report_generated_at = Column(DateTime)
    html_analysis = Column(Text)  # 存储HTML文件的AI分析结果
    html_approved = Column(Integer, default=0)  # HTML审核状态：0=待审核，1=已通过，-1=已拒绝
    html_approved_by = Column(Integer, ForeignKey('user.id'), nullable=True)  # 审核人ID
    html_approved_at = Column(DateTime, nullable=True)  # 审核时间
    html_review_note = Column(Text)
    rate_limit_log = Column(Text)
    custom_prompt = Column(Text)  # 用户自定义的分析提示词（已废弃，保留用于兼容）
    user_prompt_template = Column(Text)  # 用户自定义的提示词模板（不包含数据部分）
    is_featured = Column(Boolean, default=False)  # 是否加精
    color_tag = Column(String(20), nullable=True)  # 任务卡片颜色标签（如 'blue', 'green'）
    # 协作功能字段
    organization_id = Column(Integer, ForeignKey('organization.id'), nullable=True)  # 所属组织
    sharing_type = Column(String(20), default='private')  # private私有/shared共享/organization组织/public公开
    public_approved = Column(Integer, default=0)  # 项目公开审核：0=待审核 1=已通过 -1=已拒绝，仅当 sharing_type=public 时生效
    like_count = Column(Integer, default=0)  # 点赞数（公开项目用）
    # 多HTML文件支持
    html_files = Column(Text)  # JSON格式存储多个HTML文件: [{"name": "file.html", "path": "/path/to/file.html"}, ...]
    # 外部URL与教程链接（扣子/豆包等分享链接）
    share_url = Column(String(500))  # 扣子编程、豆包编程等提供的分享URL
    tutorial_link = Column(String(500))  # 提示词分享链接，便于分享和后期查找
    submission_manage_code = Column(String(64), nullable=True)  # 任务级删改认证码；为空表示未启用
    ai_generated = Column(Boolean, default=False)  # 是否为一键生成任务
    html_ai_edit_remaining = Column(Integer, nullable=True)  # 剩余可修改次数（3→2→1→0），非一键生成为 None
    # 一键生成异步：pending=后台生成中；failed=失败（见 oneclick_generation_error）；成功后可置 NULL
    oneclick_generation_status = Column(String(20), nullable=True)
    oneclick_generation_error = Column(Text, nullable=True)
    # 数据大屏（Smart Analyze Step 1）：生成/修改次数与生成文件
    dashboard_file_name = Column(String(200), nullable=True)  # 原始文件名（用于展示）
    dashboard_saved_name = Column(String(260), nullable=True)  # static/uploads 下保存名（URL 稳定）
    dashboard_generated_at = Column(DateTime, nullable=True)
    dashboard_ai_edit_remaining = Column(Integer, nullable=True)  # 默认 3；每次自动生成/修改消耗 1 次
    dashboard_generation_status = Column(String(20), nullable=True)  # pending/completed/failed
    dashboard_generation_error = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)  # 任务状态：True=正常接收/读取数据，False=停用（接口拒绝）
    # 单任务 API 用量（GET /api/<task_id> 与 GET /api/<task_id>/all）；限额由常量 + quota_extra_* 组成
    api_task_get_count = Column(Integer, default=0)  # 轻量 GET（最新 3 条）成功次数
    api_task_all_count = Column(Integer, default=0)  # /all 成功次数（计入读取次数上限）
    api_task_all_bytes_total = Column(BigInteger, default=0)  # /all 已下发响应体累计字节
    quota_extra_all_reads = Column(Integer, default=0)  # 管理员批复的额外「/all 读取次数」
    quota_extra_all_bytes = Column(BigInteger, default=0)  # 管理员批复的额外流量额度（字节）
    # 单任务“提交写入”用量与限额（用于防刷库/防存爆；限额 = SiteQuotaDefault 默认值 + quota_extra_submit_*）
    submission_count_total = Column(Integer, default=0)  # 已接收提交条数（累计）
    submission_bytes_total = Column(BigInteger, default=0)  # 已接收提交 data 的累计字节（UTF-8）
    quota_extra_submit_count = Column(Integer, default=0)  # 管理员批复的额外「提交条数」额度
    quota_extra_submit_bytes = Column(BigInteger, default=0)  # 管理员批复的额外「提交字节」额度（字节）
    approver = relationship('User', foreign_keys=[html_approved_by], backref='approved_tasks')
    organization = relationship('Organization', back_populates='tasks')
    shares = relationship('TaskShare', back_populates='task', cascade='all, delete-orphan')
    likes = relationship('TaskLike', back_populates='task', cascade='all, delete-orphan')
    quota_requests = relationship('TaskQuotaRequest', back_populates='task', cascade='all, delete-orphan')


class Submission(Base):
    __tablename__ = 'submission'
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('task.id', ondelete='CASCADE'))  # 数据库层面级联删除
    task = relationship('Task', back_populates='submission')
    data = Column(Text, nullable=False)
    submitted_at = Column(DateTime, default=datetime.now)


class ApiAccessLog(Base):
    __tablename__ = 'api_access_log'
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('task.id', ondelete='CASCADE'), nullable=True)
    endpoint = Column(String(100), nullable=False)
    response_bytes = Column(Integer, default=0)
    client_ip = Column(String(100))
    created_at = Column(DateTime, default=datetime.now)


class AIConfig(Base):
    __tablename__ = 'ai_config'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id'), unique=True)
    user = relationship('User', back_populates='ai_config')
    selected_model = Column(String(50), default='deepseek')
    # 各平台 key/token 在加密后可能明显变长，统一使用 TEXT 避免长度不足
    deepseek_api_key = Column(Text)
    doubao_api_key = Column(Text)
    doubao_secret_key = Column(Text)
    qwen_api_key = Column(Text)
    # 硅基流动（ChatServer）配置
    chat_server_api_url = Column(String(200))
    chat_server_api_token = Column(Text)
    # 更多模型
    moonshot_api_key = Column(Text)
    glm_api_key = Column(Text)
    ernie_api_key = Column(Text)
    ernie_secret_key = Column(Text)
    openrouter_api_key = Column(Text)


class CertificationRequest(Base):
    __tablename__ = 'certification_request'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    status = Column(Integer, default=0)  # 0=待审核 1=已通过 -1=已拒绝
    file_name = Column(String(255))
    file_path = Column(String(500))
    created_at = Column(DateTime, default=datetime.now)
    reviewed_at = Column(DateTime)
    reviewed_by = Column(Integer, ForeignKey('user.id'))
    review_note = Column(Text)

    user = relationship('User', back_populates='certification_requests', foreign_keys=[user_id])
    reviewer = relationship('User', foreign_keys=[reviewed_by], backref='processed_certification_requests')


class Post(Base):
    """留言板帖子（提问）"""
    __tablename__ = 'post'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    # 管理员置顶：用于在 community 留言列表中置顶显示
    is_pinned = Column(Boolean, default=False)
    pinned_at = Column(DateTime, nullable=True)
    
    user = relationship('User', foreign_keys=[user_id])
    replies = relationship('PostReply', back_populates='post', cascade='all, delete-orphan', order_by='PostReply.created_at')


class PostReply(Base):
    """留言回复（针对某条留言的回答）"""
    __tablename__ = 'post_reply'
    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey('post.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    
    post = relationship('Post', back_populates='replies')
    user = relationship('User', foreign_keys=[user_id])


class Organization(Base):
    """组织/团队"""
    __tablename__ = 'organization'
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    org_code = Column(String(50), unique=True, nullable=False)  # 组织代码，五位大写字母数字，由创建时生成
    creator_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    members_can_edit_tasks = Column(Boolean, default=False)  # 组织成员对组织内任务的权限：False=只读（默认），True=可编辑
    # 入驻团队页（首页公开）展示：需管理员审核；默认不申请、未通过则不出现在 /teams
    teams_public_requested = Column(Boolean, default=False)  # 是否已申请在「入驻团队」公开展示
    teams_public_approved = Column(Integer, default=0)  # 0=未申请或待审核(已申请时), 1=已通过, -1=已拒绝
    
    creator = relationship('User', foreign_keys=[creator_id])
    members = relationship('OrganizationMember', back_populates='organization', cascade='all, delete-orphan')
    tasks = relationship('Task', back_populates='organization')


class OrganizationMember(Base):
    """组织成员"""
    __tablename__ = 'organization_member'
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organization.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    joined_at = Column(DateTime, default=datetime.now)
    role = Column(String(20), default='member')  # member普通成员, admin管理员
    
    organization = relationship('Organization', back_populates='members')
    user = relationship('User', foreign_keys=[user_id])


class TaskShare(Base):
    """任务共享"""
    __tablename__ = 'task_share'
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('task.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    shared_at = Column(DateTime, default=datetime.now)
    can_edit = Column(Boolean, default=False)  # 是否可编辑；默认只读，共享时可选择「编辑」
    
    task = relationship('Task', back_populates='shares')
    user = relationship('User', foreign_keys=[user_id])


class TaskLike(Base):
    """公开任务点赞（仅对 sharing_type=public 的任务）"""
    __tablename__ = 'task_like'
    __table_args__ = (UniqueConstraint('task_id', 'user_id', name='uq_task_like_task_user'),)
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('task.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    
    task = relationship('Task', back_populates='likes')
    user = relationship('User', foreign_keys=[user_id])


class TaskQuotaRequest(Base):
    """任务 /all 限额「解除/加额」申请（管理员批复额外次数与流量）"""
    __tablename__ = 'task_quota_request'
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey('task.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    status = Column(Integer, default=0)  # 0=待审核 1=已通过 -1=已拒绝
    applicant_note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    reviewed_at = Column(DateTime)
    reviewed_by = Column(Integer, ForeignKey('user.id'))
    review_note = Column(Text)
    requested_extra_reads = Column(Integer)  # 申请：希望增加的 /all 次数额度
    requested_extra_mb = Column(Integer)  # 申请：希望增加的流量额度（MB）
    granted_extra_reads = Column(Integer)  # 批复：增加的 /all 次数额度
    granted_extra_mb = Column(Integer)  # 批复：增加的流量额度（MB）

    task = relationship('Task', back_populates='quota_requests')
    applicant = relationship('User', foreign_keys=[user_id])
    reviewer = relationship('User', foreign_keys=[reviewed_by])


class SiteQuotaDefault(Base):
    """全站默认：每个任务 /all 接口的基础次数与流量上限（单表单行 id=1，管理员后台可改）"""
    __tablename__ = 'site_quota_default'
    id = Column(Integer, primary_key=True)
    default_all_read_limit = Column(Integer, nullable=False, default=2000)
    default_all_bytes_limit = Column(BigInteger, nullable=False, default=100 * 1024 * 1024)
    # 单任务提交写入默认限额（防刷库/防存爆）
    # - default_submit_count_limit = 0 表示「不限提交次数」（仅按累计体积/单条大小等限制）。
    default_submit_count_limit = Column(Integer, nullable=False, default=100000)
    # 默认把累计体积上限调高一些（仍建议结合 Nginx/WAF 做全站限速）
    default_submit_bytes_limit = Column(BigInteger, nullable=False, default=500 * 1024 * 1024)  # 默认 500MB
    auto_quota_approve_enabled = Column(Integer, nullable=False, default=0)  # 0=关闭 1=开启
    auto_quota_approve_max_reads = Column(Integer, nullable=False, default=0)  # 自动审批：次数阈值
    auto_quota_approve_max_mb = Column(Integer, nullable=False, default=0)  # 自动审批：流量阈值(MB)
    updated_at = Column(DateTime, default=datetime.now)


class OneclickPromptOption(Base):
    """一键生成任务：勾选后追加给模型的说明文案（管理员后台可改）；正文中的「API地址」会在生成时替换为真实接口根 URL"""

    __tablename__ = 'oneclick_prompt_option'
    id = Column(Integer, primary_key=True)
    opt_key = Column(String(64), unique=True, nullable=False)
    label = Column(String(200), nullable=False)
    body = Column(Text, nullable=False, default='')
    sort_order = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=datetime.now)


# 一键生成默认追加说明（与 DB 初始种子一致；代码回退用）
DEFAULT_ONECLICK_PROMPT_OPTIONS = [
    (
        'opt_upload',
        '数据上传',
        '收数方式：用户填写后通过 POST 向「API地址」提交 JSON（建议 Content-Type: application/json），与 QuickForm 数据接口约定一致。',
    ),
    (
        'opt_fetch',
        '数据获取',
        '若需在页面上展示已收集的数据：使用 GET 请求「API地址/all」，响应为 JSON 对象；列表在 submissions 字段（数组），总条数在 total_submissions 字段。展示或统计时请使用上述字段，勿将根对象当作数组对其求 length。',
    ),
    (
        'opt_responsive',
        '响应式布局',
        '布局需适配手机与桌面：主要区域与按钮在小屏上不换行挤压，字号与触控区域足够，可用弹性布局或简单栅格实现自适应。',
    ),
    (
        'opt_validate',
        '表单校验',
        '在提交前校验必填项与基本格式（如邮箱、数字范围等），错误时用就近文案或边框高亮提示，避免静默失败。',
    ),
    (
        'opt_success_tip',
        '提交成功提示',
        '提交成功后给出明确反馈（如简短提示文案或非阻塞提示），并可按需清空表单以便连续填报。',
    ),
    (
        'opt_decorate',
        '内置页面装饰',
        '内置轻量样式即可：标题与表单分区清晰，卡片或表单区适度圆角、留白与浅阴影，配色简洁，保证可读、不花哨。',
    ),
    (
        'opt_manage_code',
        '删改功能（认证码）',
        '补充一个“删改认证码（manage code）”可选输入框（默认隐藏/密码态），用于教师在需要时删除、清空或修改已提交数据。相关请求需携带认证码：可放在 query 参数 edit_code，或请求头 X-QuickForm-Edit-Code。请在页面上给出简短提示：该认证码应由任务创建者在任务详情页生成并妥善保管，不可暴露给学生。',
    ),
]


def migrate_database(engine):
    """数据库迁移函数"""
    try:
        if (os.getenv('QF_SKIP_DB_MIGRATION') or '').strip() == '1':
            logger.warning("检测到 QF_SKIP_DB_MIGRATION=1：跳过数据库迁移（仅建议在确认库结构已完整时使用）")
            return
        inspector = inspect(engine)
        columns = [col['name'] for col in inspector.get_columns('user')]
        ai_cfg_cols = [col['name'] for col in inspector.get_columns('ai_config')] if 'ai_config' in inspector.get_table_names() else []
        task_cols = [col['name'] for col in inspector.get_columns('task')] if 'task' in inspector.get_table_names() else []
        cert_req_cols = [col['name'] for col in inspector.get_columns('certification_request')] if 'certification_request' in inspector.get_table_names() else []
        org_cols = [col['name'] for col in inspector.get_columns('organization')] if 'organization' in inspector.get_table_names() else []
        post_cols = [col['name'] for col in inspector.get_columns('post')] if 'post' in inspector.get_table_names() else []
        
        with engine.begin() as conn:
            if 'school' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN school VARCHAR(200)"))
                    logger.info("成功添加school字段到user表")
                except Exception as e:
                    logger.warning(f"添加school字段失败（可能已存在）: {str(e)}")
            
            if 'school_province' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN school_province VARCHAR(50)"))
                    logger.info("成功为user表添加school_province字段")
                except Exception as e:
                    logger.warning(f"添加school_province字段失败（可能已存在）: {str(e)}")
            
            if 'school_province_source' not in columns:
                try:
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    # MySQL: VARCHAR DEFAULT；SQLite: 也支持 DEFAULT
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE user ADD COLUMN school_province_source VARCHAR(20) DEFAULT 'auto'"))
                    else:
                        conn.execute(text("ALTER TABLE user ADD COLUMN school_province_source VARCHAR(20) DEFAULT 'auto'"))
                    conn.execute(text("UPDATE user SET school_province_source = 'auto' WHERE school_province_source IS NULL"))
                    logger.info("成功为user表添加school_province_source字段")
                except Exception as e:
                    logger.warning(f"添加school_province_source字段失败（可能已存在）: {str(e)}")
            
            if 'phone' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN phone VARCHAR(20)"))
                    logger.info("成功添加phone字段到user表")
                except Exception as e:
                    logger.warning(f"添加phone字段失败（可能已存在）: {str(e)}")
            
            if 'role' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN role VARCHAR(20) DEFAULT 'user'"))
                    conn.execute(text("UPDATE user SET role = 'user' WHERE role IS NULL"))
                    logger.info("成功添加role字段到user表")
                except Exception as e:
                    logger.warning(f"添加role字段失败（可能已存在）: {str(e)}")
            
            if 'task_limit' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN task_limit INTEGER DEFAULT 3"))
                    conn.execute(text("UPDATE user SET task_limit = 3 WHERE task_limit IS NULL"))
                    logger.info("成功添加task_limit字段到user表")
                except Exception as e:
                    logger.warning(f"添加task_limit字段失败（可能已存在）: {str(e)}")

            if 'is_certified' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN is_certified BOOLEAN DEFAULT 0"))
                    logger.info("成功为user表添加is_certified字段")
                except Exception as e:
                    logger.warning(f"添加is_certified字段失败（可能已存在）: {str(e)}")

            if 'certified_at' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN certified_at DATETIME"))
                    logger.info("成功为user表添加certified_at字段")
                except Exception as e:
                    logger.warning(f"添加certified_at字段失败（可能已存在）: {str(e)}")

            if 'certification_note' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN certification_note TEXT"))
                    logger.info("成功为user表添加certification_note字段")
                except Exception as e:
                    logger.warning(f"添加certification_note字段失败（可能已存在）: {str(e)}")

            if 'email_verified' not in columns:
                try:
                    # MySQL: TINYINT(1)；SQLite: BOOLEAN。已有用户默认 1 视为已验证
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE user ADD COLUMN email_verified TINYINT(1) DEFAULT 1"))
                    else:
                        conn.execute(text("ALTER TABLE user ADD COLUMN email_verified BOOLEAN DEFAULT 1"))
                    conn.execute(text("UPDATE user SET email_verified = 1 WHERE email_verified IS NULL"))
                    logger.info("成功为user表添加email_verified字段")
                except Exception as e:
                    logger.warning(f"添加email_verified字段失败（可能已存在）: {str(e)}")
            
            # ai_config 新增 chat_server 字段
            if ai_cfg_cols and 'chat_server_api_url' not in ai_cfg_cols:
                try:
                    conn.execute(text("ALTER TABLE ai_config ADD COLUMN chat_server_api_url VARCHAR(200)"))
                    logger.info("成功为ai_config添加chat_server_api_url")
                except Exception as e:
                    logger.warning(f"添加chat_server_api_url失败（可能已存在）: {str(e)}")
            if ai_cfg_cols and 'chat_server_api_token' not in ai_cfg_cols:
                try:
                    conn.execute(text("ALTER TABLE ai_config ADD COLUMN chat_server_api_token VARCHAR(200)"))
                    logger.info("成功为ai_config添加chat_server_api_token")
                except Exception as e:
                    logger.warning(f"添加chat_server_api_token失败（可能已存在）: {str(e)}")

            # ai_config 各 key/token：加密后字符串可能较长，MySQL 下统一升级为 TEXT
            if ai_cfg_cols:
                try:
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    if dialect == 'mysql':
                        key_cols_to_text = [
                            'deepseek_api_key',
                            'doubao_api_key',
                            'doubao_secret_key',
                            'qwen_api_key',
                            'chat_server_api_token',
                            'moonshot_api_key',
                            'glm_api_key',
                            'ernie_api_key',
                            'ernie_secret_key',
                            'openrouter_api_key',
                        ]
                        for col_name in key_cols_to_text:
                            if col_name in ai_cfg_cols:
                                try:
                                    conn.execute(text(f"ALTER TABLE ai_config MODIFY COLUMN {col_name} TEXT"))
                                except Exception:
                                    pass
                        logger.info("成功将 ai_config 的 key/token 字段升级为 TEXT")
                except Exception as e:
                    logger.warning(f"升级 ai_config key/token 为 TEXT 失败（可能已升级）: {str(e)}")
            
            # task 新增 html_analysis 字段
            if task_cols and 'html_analysis' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN html_analysis TEXT"))
                    logger.info("成功为task添加html_analysis字段")
                except Exception as e:
                    logger.warning(f"添加html_analysis失败（可能已存在）: {str(e)}")
            
            # task 新增审核相关字段
            if task_cols and 'html_approved' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN html_approved INTEGER DEFAULT 0"))
                    logger.info("成功为task添加html_approved字段")
                except Exception as e:
                    logger.warning(f"添加html_approved失败（可能已存在）: {str(e)}")
            if task_cols and 'html_approved_by' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN html_approved_by INTEGER"))
                    logger.info("成功为task添加html_approved_by字段")
                except Exception as e:
                    logger.warning(f"添加html_approved_by失败（可能已存在）: {str(e)}")
            if task_cols and 'html_approved_at' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN html_approved_at DATETIME"))
                    logger.info("成功为task添加html_approved_at字段")
                except Exception as e:
                    logger.warning(f"添加html_approved_at失败（可能已存在）: {str(e)}")

            if task_cols and 'html_review_note' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN html_review_note TEXT"))
                    logger.info("成功为task添加html_review_note字段")
                except Exception as e:
                    logger.warning(f"添加html_review_note失败（可能已存在）: {str(e)}")

            if task_cols and 'rate_limit_log' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN rate_limit_log TEXT"))
                    logger.info("成功为task添加rate_limit_log字段")
                except Exception as e:
                    logger.warning(f"添加rate_limit_log失败（可能已存在）: {str(e)}")
            
            if task_cols and 'custom_prompt' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN custom_prompt TEXT"))
                    logger.info("成功为task添加custom_prompt字段")
                except Exception as e:
                    logger.warning(f"添加custom_prompt失败（可能已存在）: {str(e)}")
            
            if task_cols and 'user_prompt_template' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN user_prompt_template TEXT"))
                    logger.info("成功为task添加user_prompt_template字段")
                except Exception as e:
                    logger.warning(f"添加user_prompt_template失败（可能已存在）: {str(e)}")
            
            if task_cols and 'is_featured' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN is_featured BOOLEAN DEFAULT 0"))
                    logger.info("成功为task添加is_featured字段")
                except Exception as e:
                    logger.warning(f"添加is_featured失败（可能已存在）: {str(e)}")

            if task_cols and 'color_tag' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN color_tag VARCHAR(20)"))
                    logger.info("成功为task添加color_tag字段（任务卡片颜色标签）")
                except Exception as e:
                    logger.warning(f"添加color_tag失败（可能已存在）: {str(e)}")
            if task_cols and 'ai_generated' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN ai_generated BOOLEAN DEFAULT 0"))
                    logger.info("成功为task添加ai_generated字段（一键生成任务）")
                except Exception as e:
                    logger.warning(f"添加ai_generated失败（可能已存在）: {str(e)}")
            if task_cols and 'html_ai_edit_remaining' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN html_ai_edit_remaining INTEGER"))
                    logger.info("成功为task添加html_ai_edit_remaining字段")
                except Exception as e:
                    logger.warning(f"添加html_ai_edit_remaining失败（可能已存在）: {str(e)}")
            if task_cols:
                if 'oneclick_generation_status' not in task_cols:
                    try:
                        conn.execute(text("ALTER TABLE task ADD COLUMN oneclick_generation_status VARCHAR(20)"))
                        logger.info("成功为task添加oneclick_generation_status字段")
                    except Exception as e:
                        logger.warning(f"添加oneclick_generation_status失败（可能已存在）: {str(e)}")
                if 'oneclick_generation_error' not in task_cols:
                    try:
                        conn.execute(text("ALTER TABLE task ADD COLUMN oneclick_generation_error TEXT"))
                        logger.info("成功为task添加oneclick_generation_error字段")
                    except Exception as e:
                        logger.warning(f"添加oneclick_generation_error失败（可能已存在）: {str(e)}")
                # 数据大屏相关字段
                for col_name, ddl in [
                    ('dashboard_file_name', "ALTER TABLE task ADD COLUMN dashboard_file_name VARCHAR(200)"),
                    ('dashboard_saved_name', "ALTER TABLE task ADD COLUMN dashboard_saved_name VARCHAR(260)"),
                    ('dashboard_generated_at', "ALTER TABLE task ADD COLUMN dashboard_generated_at DATETIME"),
                    ('dashboard_ai_edit_remaining', "ALTER TABLE task ADD COLUMN dashboard_ai_edit_remaining INTEGER"),
                    ('dashboard_generation_status', "ALTER TABLE task ADD COLUMN dashboard_generation_status VARCHAR(20)"),
                    ('dashboard_generation_error', "ALTER TABLE task ADD COLUMN dashboard_generation_error TEXT"),
                ]:
                    if col_name not in task_cols:
                        try:
                            conn.execute(text(ddl))
                            logger.info(f"成功为task添加{col_name}字段（数据大屏）")
                        except Exception as e:
                            logger.warning(f"添加{col_name}失败（可能已存在）: {str(e)}")
            if task_cols and 'is_active' not in task_cols:
                try:
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE task ADD COLUMN is_active TINYINT(1) DEFAULT 1"))
                    else:
                        conn.execute(text("ALTER TABLE task ADD COLUMN is_active BOOLEAN DEFAULT 1"))
                    conn.execute(text("UPDATE task SET is_active = 1 WHERE is_active IS NULL"))
                    logger.info("成功为task添加is_active字段（任务正常/停用）")
                except Exception as e:
                    logger.warning(f"添加is_active失败（可能已存在）: {str(e)}")

            # 创建认证申请表
            if 'certification_request' not in inspector.get_table_names():
                try:
                    CertificationRequest.__table__.create(bind=engine)
                    logger.info("成功创建certification_request表")
                except Exception as e:
                    logger.warning(f"创建certification_request表失败: {str(e)}")
            
            # 创建留言板表
            if 'post' not in inspector.get_table_names():
                try:
                    Post.__table__.create(bind=engine)
                    logger.info("成功创建post表")
                except Exception as e:
                    logger.warning(f"创建post表失败: {str(e)}")

            # post：管理员置顶字段
            if post_cols and 'is_pinned' not in post_cols:
                try:
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE post ADD COLUMN is_pinned TINYINT(1) DEFAULT 0"))
                    else:
                        conn.execute(text("ALTER TABLE post ADD COLUMN is_pinned BOOLEAN DEFAULT 0"))
                    conn.execute(text("UPDATE post SET is_pinned = 0 WHERE is_pinned IS NULL"))
                    logger.info("成功为post添加is_pinned字段（置顶）")
                except Exception as e:
                    logger.warning(f"添加is_pinned失败（可能已存在）: {str(e)}")
            if post_cols and 'pinned_at' not in post_cols:
                try:
                    conn.execute(text("ALTER TABLE post ADD COLUMN pinned_at DATETIME"))
                    logger.info("成功为post添加pinned_at字段（置顶时间）")
                except Exception as e:
                    logger.warning(f"添加pinned_at失败（可能已存在）: {str(e)}")
            
            # 创建留言回复表
            if 'post_reply' not in inspector.get_table_names():
                try:
                    PostReply.__table__.create(bind=engine)
                    logger.info("成功创建post_reply表")
                except Exception as e:
                    logger.warning(f"创建post_reply表失败: {str(e)}")
            
            # 创建组织表
            if 'organization' not in inspector.get_table_names():
                try:
                    Organization.__table__.create(bind=engine)
                    logger.info("成功创建organization表")
                except Exception as e:
                    logger.warning(f"创建organization表失败: {str(e)}")
            
            # 创建组织成员表
            if 'organization_member' not in inspector.get_table_names():
                try:
                    OrganizationMember.__table__.create(bind=engine)
                    logger.info("成功创建organization_member表")
                except Exception as e:
                    logger.warning(f"创建organization_member表失败: {str(e)}")
            
            # 创建任务共享表
            if 'task_share' not in inspector.get_table_names():
                try:
                    TaskShare.__table__.create(bind=engine)
                    logger.info("成功创建task_share表")
                except Exception as e:
                    logger.warning(f"创建task_share表失败: {str(e)}")
            
            # 创建任务点赞表
            if 'task_like' not in inspector.get_table_names():
                try:
                    TaskLike.__table__.create(bind=engine)
                    logger.info("成功创建task_like表")
                except Exception as e:
                    logger.warning(f"创建task_like表失败: {str(e)}")

            # 创建 API 访问日志表（用于 /all 流量预警）
            if 'api_access_log' not in inspector.get_table_names():
                try:
                    ApiAccessLog.__table__.create(bind=engine)
                    logger.info("成功创建api_access_log表")
                except Exception as e:
                    logger.warning(f"创建api_access_log表失败: {str(e)}")
            
            # task 新增协作相关字段
            if task_cols and 'organization_id' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN organization_id INTEGER"))
                    logger.info("成功为task添加organization_id字段")
                except Exception as e:
                    logger.warning(f"添加organization_id失败（可能已存在）: {str(e)}")
            
            if task_cols and 'sharing_type' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN sharing_type VARCHAR(20) DEFAULT 'private'"))
                    conn.execute(text("UPDATE task SET sharing_type = 'private' WHERE sharing_type IS NULL"))
                    logger.info("成功为task添加sharing_type字段")
                except Exception as e:
                    logger.warning(f"添加sharing_type失败（可能已存在）: {str(e)}")
            
            if task_cols and 'html_files' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN html_files TEXT"))
                    logger.info("成功为task添加html_files字段")
                except Exception as e:
                    logger.warning(f"添加html_files失败（可能已存在）: {str(e)}")
            
            if task_cols and 'like_count' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN like_count INTEGER DEFAULT 0"))
                    conn.execute(text("UPDATE task SET like_count = 0 WHERE like_count IS NULL"))
                    logger.info("成功为task添加like_count字段")
                except Exception as e:
                    logger.warning(f"添加like_count失败（可能已存在）: {str(e)}")

            if task_cols and 'public_approved' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN public_approved INTEGER DEFAULT 0"))
                    conn.execute(text("UPDATE task SET public_approved = 1 WHERE sharing_type = 'public' AND (public_approved IS NULL OR public_approved = 0)"))
                    logger.info("成功为task添加public_approved字段")
                except Exception as e:
                    logger.warning(f"添加public_approved失败（可能已存在）: {str(e)}")

            if task_cols and 'share_url' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN share_url VARCHAR(500)"))
                    logger.info("成功为task添加share_url字段（扣子/豆包等分享URL）")
                except Exception as e:
                    logger.warning(f"添加share_url失败（可能已存在）: {str(e)}")
            if task_cols and 'tutorial_link' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN tutorial_link VARCHAR(500)"))
                    logger.info("成功为task添加tutorial_link字段（教程/提示词链接）")
                except Exception as e:
                    logger.warning(f"添加tutorial_link失败（可能已存在）: {str(e)}")
            if task_cols and 'submission_manage_code' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN submission_manage_code VARCHAR(64)"))
                    logger.info("成功为task添加submission_manage_code字段（删改认证码）")
                except Exception as e:
                    logger.warning(f"添加submission_manage_code失败（可能已存在）: {str(e)}")

            # task.rate_limit_log：限流日志持续拼接会超过 MySQL TEXT(64KB)，升级为 MEDIUMTEXT
            if task_cols and 'rate_limit_log' in task_cols:
                dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                if dialect == 'mysql':
                    try:
                        conn.execute(text("ALTER TABLE task MODIFY COLUMN rate_limit_log MEDIUMTEXT"))
                        logger.info("成功将 task.rate_limit_log 升级为 MEDIUMTEXT")
                    except Exception as e:
                        logger.warning(f"升级 rate_limit_log 为 MEDIUMTEXT 失败（可能已升级）: {str(e)}")
            
            # ai_config 新增更多模型字段
            for col_name in ['moonshot_api_key', 'glm_api_key', 'ernie_api_key', 'ernie_secret_key', 'openrouter_api_key']:
                if ai_cfg_cols and col_name not in ai_cfg_cols:
                    try:
                        conn.execute(text(f"ALTER TABLE ai_config ADD COLUMN {col_name} VARCHAR(200)"))
                        logger.info(f"成功为ai_config添加{col_name}")
                    except Exception as e:
                        logger.warning(f"添加{col_name}失败（可能已存在）: {str(e)}")
            
            # organization 组织成员对组织任务的权限开关：默认只读
            if org_cols and 'members_can_edit_tasks' not in org_cols:
                try:
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE organization ADD COLUMN members_can_edit_tasks TINYINT(1) DEFAULT 0"))
                    else:
                        conn.execute(text("ALTER TABLE organization ADD COLUMN members_can_edit_tasks BOOLEAN DEFAULT 0"))
                    logger.info("成功为organization添加members_can_edit_tasks字段")
                except Exception as e:
                    logger.warning(f"添加members_can_edit_tasks失败（可能已存在）: {str(e)}")
            # organization 入驻团队（首页公开）审核
            if org_cols and 'teams_public_requested' not in org_cols:
                try:
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE organization ADD COLUMN teams_public_requested TINYINT(1) DEFAULT 0"))
                    else:
                        conn.execute(text("ALTER TABLE organization ADD COLUMN teams_public_requested BOOLEAN DEFAULT 0"))
                    logger.info("成功为organization添加teams_public_requested字段")
                except Exception as e:
                    logger.warning(f"添加teams_public_requested失败（可能已存在）: {str(e)}")
            if org_cols and 'teams_public_approved' not in org_cols:
                try:
                    conn.execute(text("ALTER TABLE organization ADD COLUMN teams_public_approved INTEGER DEFAULT 0"))
                    logger.info("成功为organization添加teams_public_approved字段")
                except Exception as e:
                    logger.warning(f"添加teams_public_approved失败（可能已存在）: {str(e)}")

            # task：单任务 API 用量与 /all 加额（限额 = 系统默认 + quota_extra_*）
            dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
            backfill_task_api_from_log = False
            if task_cols and 'api_task_get_count' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN api_task_get_count INTEGER DEFAULT 0"))
                    logger.info("成功为task添加api_task_get_count")
                except Exception as e:
                    logger.warning(f"添加api_task_get_count失败（可能已存在）: {str(e)}")
            if task_cols and 'api_task_all_count' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN api_task_all_count INTEGER DEFAULT 0"))
                    logger.info("成功为task添加api_task_all_count")
                    backfill_task_api_from_log = True
                except Exception as e:
                    logger.warning(f"添加api_task_all_count失败（可能已存在）: {str(e)}")
            if task_cols and 'api_task_all_bytes_total' not in task_cols:
                try:
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE task ADD COLUMN api_task_all_bytes_total BIGINT DEFAULT 0"))
                    else:
                        conn.execute(text("ALTER TABLE task ADD COLUMN api_task_all_bytes_total INTEGER DEFAULT 0"))
                    logger.info("成功为task添加api_task_all_bytes_total")
                except Exception as e:
                    logger.warning(f"添加api_task_all_bytes_total失败（可能已存在）: {str(e)}")
            if task_cols and 'quota_extra_all_reads' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN quota_extra_all_reads INTEGER DEFAULT 0"))
                    logger.info("成功为task添加quota_extra_all_reads")
                except Exception as e:
                    logger.warning(f"添加quota_extra_all_reads失败（可能已存在）: {str(e)}")
            if task_cols and 'quota_extra_all_bytes' not in task_cols:
                try:
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE task ADD COLUMN quota_extra_all_bytes BIGINT DEFAULT 0"))
                    else:
                        conn.execute(text("ALTER TABLE task ADD COLUMN quota_extra_all_bytes INTEGER DEFAULT 0"))
                    logger.info("成功为task添加quota_extra_all_bytes")
                except Exception as e:
                    logger.warning(f"添加quota_extra_all_bytes失败（可能已存在）: {str(e)}")

            # task：提交写入限额与计数（防刷库/防存爆）
            if task_cols and 'submission_count_total' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN submission_count_total INTEGER DEFAULT 0"))
                    conn.execute(text("UPDATE task SET submission_count_total = 0 WHERE submission_count_total IS NULL"))
                    logger.info("成功为task添加submission_count_total")
                except Exception as e:
                    logger.warning(f"添加submission_count_total失败（可能已存在）: {str(e)}")
            if task_cols and 'submission_bytes_total' not in task_cols:
                try:
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE task ADD COLUMN submission_bytes_total BIGINT DEFAULT 0"))
                    else:
                        conn.execute(text("ALTER TABLE task ADD COLUMN submission_bytes_total INTEGER DEFAULT 0"))
                    conn.execute(text("UPDATE task SET submission_bytes_total = 0 WHERE submission_bytes_total IS NULL"))
                    logger.info("成功为task添加submission_bytes_total")
                except Exception as e:
                    logger.warning(f"添加submission_bytes_total失败（可能已存在）: {str(e)}")
            if task_cols and 'quota_extra_submit_count' not in task_cols:
                try:
                    conn.execute(text("ALTER TABLE task ADD COLUMN quota_extra_submit_count INTEGER DEFAULT 0"))
                    conn.execute(text("UPDATE task SET quota_extra_submit_count = 0 WHERE quota_extra_submit_count IS NULL"))
                    logger.info("成功为task添加quota_extra_submit_count")
                except Exception as e:
                    logger.warning(f"添加quota_extra_submit_count失败（可能已存在）: {str(e)}")
            if task_cols and 'quota_extra_submit_bytes' not in task_cols:
                try:
                    if dialect == 'mysql':
                        conn.execute(text("ALTER TABLE task ADD COLUMN quota_extra_submit_bytes BIGINT DEFAULT 0"))
                    else:
                        conn.execute(text("ALTER TABLE task ADD COLUMN quota_extra_submit_bytes INTEGER DEFAULT 0"))
                    conn.execute(text("UPDATE task SET quota_extra_submit_bytes = 0 WHERE quota_extra_submit_bytes IS NULL"))
                    logger.info("成功为task添加quota_extra_submit_bytes")
                except Exception as e:
                    logger.warning(f"添加quota_extra_submit_bytes失败（可能已存在）: {str(e)}")

            # 从 submission 聚合回填「提交条数 / 累计字节」（升级后首次对齐；后续由接口累加维护）
            if task_cols and 'submission' in inspector.get_table_names():
                if (os.getenv('QF_SKIP_DB_BACKFILL') or '').strip() == '1':
                    logger.warning("检测到 QF_SKIP_DB_BACKFILL=1：跳过 task 提交用量回填（submission_count_total / submission_bytes_total）")
                else:
                    try:
                        if dialect == 'mysql':
                            conn.execute(text("""
                                UPDATE task AS t
                                LEFT JOIN (
                                    SELECT task_id AS tid,
                                           COUNT(*) AS c,
                                           COALESCE(SUM(LENGTH(data)), 0) AS b
                                    FROM submission
                                    GROUP BY task_id
                                ) AS s ON s.tid = t.id
                                SET
                                    t.submission_count_total = COALESCE(s.c, 0),
                                    t.submission_bytes_total = COALESCE(s.b, 0)
                                WHERE COALESCE(t.submission_count_total, 0) = 0
                                  AND COALESCE(t.submission_bytes_total, 0) = 0
                                  AND EXISTS (SELECT 1 FROM submission x WHERE x.task_id = t.id LIMIT 1)
                            """))
                        else:
                            conn.execute(text("""
                                UPDATE task SET
                                    submission_count_total = COALESCE((
                                        SELECT COUNT(*) FROM submission WHERE submission.task_id = task.id
                                    ), 0),
                                    submission_bytes_total = COALESCE((
                                        SELECT SUM(LENGTH(data)) FROM submission WHERE submission.task_id = task.id
                                    ), 0)
                                WHERE COALESCE(submission_count_total, 0) = 0
                                  AND COALESCE(submission_bytes_total, 0) = 0
                                  AND EXISTS (SELECT 1 FROM submission WHERE submission.task_id = task.id)
                            """))
                        logger.info("已回填 task 的提交用量计数（submission_count_total / submission_bytes_total）")
                    except Exception as e:
                        logger.warning(f"回填 task 提交用量失败（可忽略）: {str(e)}")

            if backfill_task_api_from_log and 'api_access_log' in inspector.get_table_names():
                try:
                    if dialect == 'mysql':
                        conn.execute(text("""
                            UPDATE task AS t SET
                                t.api_task_all_count = COALESCE((
                                    SELECT COUNT(*) FROM api_access_log AS l
                                    WHERE l.task_id = t.id AND l.endpoint = 'api_task_all'
                                ), 0),
                                t.api_task_all_bytes_total = COALESCE((
                                    SELECT SUM(l.response_bytes) FROM api_access_log AS l
                                    WHERE l.task_id = t.id AND l.endpoint = 'api_task_all'
                                ), 0)
                        """))
                    else:
                        conn.execute(text("""
                            UPDATE task SET
                                api_task_all_count = COALESCE((
                                    SELECT COUNT(*) FROM api_access_log
                                    WHERE api_access_log.task_id = task.id
                                      AND api_access_log.endpoint = 'api_task_all'
                                ), 0),
                                api_task_all_bytes_total = COALESCE((
                                    SELECT SUM(api_access_log.response_bytes) FROM api_access_log
                                    WHERE api_access_log.task_id = task.id
                                      AND api_access_log.endpoint = 'api_task_all'
                                ), 0)
                        """))
                    logger.info("已从 api_access_log 回填 task 的 /all 用量计数")
                except Exception as e:
                    logger.warning(f"回填 task /all 用量失败（可忽略）: {str(e)}")

            if 'task_quota_request' not in inspector.get_table_names():
                try:
                    TaskQuotaRequest.__table__.create(bind=engine)
                    logger.info("成功创建task_quota_request表")
                except Exception as e:
                    logger.warning(f"创建task_quota_request表失败: {str(e)}")
            else:
                try:
                    quota_req_cols = [col['name'] for col in inspector.get_columns('task_quota_request')]
                    if 'requested_extra_reads' not in quota_req_cols:
                        conn.execute(text("ALTER TABLE task_quota_request ADD COLUMN requested_extra_reads INTEGER"))
                    if 'requested_extra_mb' not in quota_req_cols:
                        conn.execute(text("ALTER TABLE task_quota_request ADD COLUMN requested_extra_mb INTEGER"))
                except Exception as e:
                    logger.warning(f"更新 task_quota_request 字段失败（可忽略）: {str(e)}")

            if 'site_quota_default' not in inspector.get_table_names():
                try:
                    SiteQuotaDefault.__table__.create(bind=engine)
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    seed_bytes = 100 * 1024 * 1024
                    seed_submit_count = 100000
                    seed_submit_bytes = 500 * 1024 * 1024
                    if dialect == 'mysql':
                        conn.execute(
                            text(
                                "INSERT INTO site_quota_default "
                                "(id, default_all_read_limit, default_all_bytes_limit, default_submit_count_limit, default_submit_bytes_limit, updated_at) "
                                "VALUES (1, 2000, :b, :sc, :sb, NOW())"
                            ),
                            {"b": seed_bytes, "sc": seed_submit_count, "sb": seed_submit_bytes},
                        )
                    else:
                        conn.execute(
                            text(
                                "INSERT INTO site_quota_default "
                                "(id, default_all_read_limit, default_all_bytes_limit, default_submit_count_limit, default_submit_bytes_limit, updated_at) "
                                "VALUES (1, 2000, :b, :sc, :sb, CURRENT_TIMESTAMP)"
                            ),
                            {"b": seed_bytes, "sc": seed_submit_count, "sb": seed_submit_bytes},
                        )
                    logger.info("成功创建 site_quota_default 表并写入默认限额")
                except Exception as e:
                    logger.warning(f"创建 site_quota_default 失败: {str(e)}")
            else:
                try:
                    site_quota_cols = [col['name'] for col in inspector.get_columns('site_quota_default')]
                    if 'default_submit_count_limit' not in site_quota_cols:
                        conn.execute(text("ALTER TABLE site_quota_default ADD COLUMN default_submit_count_limit INTEGER DEFAULT 100000"))
                    if 'default_submit_bytes_limit' not in site_quota_cols:
                        # SQLite 统一 INTEGER；MySQL 用 BIGINT
                        if dialect == 'mysql':
                            conn.execute(text("ALTER TABLE site_quota_default ADD COLUMN default_submit_bytes_limit BIGINT DEFAULT 524288000"))
                        else:
                            conn.execute(text("ALTER TABLE site_quota_default ADD COLUMN default_submit_bytes_limit INTEGER DEFAULT 524288000"))
                    if 'auto_quota_approve_enabled' not in site_quota_cols:
                        conn.execute(text("ALTER TABLE site_quota_default ADD COLUMN auto_quota_approve_enabled INTEGER DEFAULT 0"))
                    if 'auto_quota_approve_max_reads' not in site_quota_cols:
                        conn.execute(text("ALTER TABLE site_quota_default ADD COLUMN auto_quota_approve_max_reads INTEGER DEFAULT 0"))
                    if 'auto_quota_approve_max_mb' not in site_quota_cols:
                        conn.execute(text("ALTER TABLE site_quota_default ADD COLUMN auto_quota_approve_max_mb INTEGER DEFAULT 0"))
                except Exception as e:
                    logger.warning(f"更新 site_quota_default 字段失败（可忽略）: {str(e)}")

            # 一键生成：追加说明文案（管理员可后台编辑）
            if 'oneclick_prompt_option' not in inspector.get_table_names():
                try:
                    OneclickPromptOption.__table__.create(bind=engine)
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    for i, (k, lab, bod) in enumerate(DEFAULT_ONECLICK_PROMPT_OPTIONS):
                        if dialect == 'mysql':
                            conn.execute(
                                text(
                                    "INSERT INTO oneclick_prompt_option "
                                    "(opt_key, label, body, sort_order, updated_at) "
                                    "VALUES (:k, :l, :b, :o, NOW())"
                                ),
                                {'k': k, 'l': lab, 'b': bod, 'o': i},
                            )
                        else:
                            conn.execute(
                                text(
                                    "INSERT INTO oneclick_prompt_option "
                                    "(opt_key, label, body, sort_order, updated_at) "
                                    "VALUES (:k, :l, :b, :o, CURRENT_TIMESTAMP)"
                                ),
                                {'k': k, 'l': lab, 'b': bod, 'o': i},
                            )
                    logger.info('成功创建 oneclick_prompt_option 表并写入默认追加说明')
                except Exception as e:
                    logger.warning(f'创建 oneclick_prompt_option 失败: {str(e)}')
            else:
                try:
                    existing_keys = {
                        row[0]
                        for row in conn.execute(text('SELECT opt_key FROM oneclick_prompt_option')).fetchall()
                    }
                    dialect = engine.dialect.name if hasattr(engine, 'dialect') else 'sqlite'
                    for i, (k, lab, bod) in enumerate(DEFAULT_ONECLICK_PROMPT_OPTIONS):
                        if k in existing_keys:
                            continue
                        if dialect == 'mysql':
                            conn.execute(
                                text(
                                    "INSERT INTO oneclick_prompt_option "
                                    "(opt_key, label, body, sort_order, updated_at) "
                                    "VALUES (:k, :l, :b, :o, NOW())"
                                ),
                                {'k': k, 'l': lab, 'b': bod, 'o': i},
                            )
                        else:
                            conn.execute(
                                text(
                                    "INSERT INTO oneclick_prompt_option "
                                    "(opt_key, label, body, sort_order, updated_at) "
                                    "VALUES (:k, :l, :b, :o, CURRENT_TIMESTAMP)"
                                ),
                                {'k': k, 'l': lab, 'b': bod, 'o': i},
                            )
                        logger.info('已补充一键生成默认选项: %s', k)
                except Exception as e:
                    logger.warning(f'补全 oneclick_prompt_option 记录失败（可忽略）: {str(e)}')
    except Exception as e:
        logger.error(f"数据库迁移失败: {str(e)}")
