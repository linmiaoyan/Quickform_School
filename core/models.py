"""数据库模型定义（校园版新库由 SQLAlchemy create_all 创建表结构）"""
from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from flask_login import UserMixin
from datetime import datetime
import uuid
import secrets
import string
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
    task_limit = Column(Integer, default=-1)  # 校园版：不限制任务数；-1 表示无限制
    email_verified = Column(Boolean, default=False)  # 创建第二个任务前需验证邮箱
    # QFLink v2（QFLink 登录）
    qflink_uid = Column(String(128), unique=True, nullable=True)  # 在线端用户唯一标识（如 uid/open_id）
    qflink_only = Column(Boolean, default=False)  # 嘉宾用户：仅允许 QFLink 登录
    qflink_disabled = Column(Boolean, default=False)  # 管理员可禁用单个 QFLink 用户
    qflink_multimodal_enabled = Column(Boolean, default=False)  # QFLink 用户是否允许多模态附件（API/任务详情）
    created_at = Column(DateTime, default=datetime.now)
    tasks = relationship('Task', back_populates='author', foreign_keys='Task.user_id', cascade='all, delete-orphan')
    ai_config = relationship('AIConfig', back_populates='user', uselist=False, cascade='all, delete-orphan')
    
    def is_admin(self):
        """检查用户是否为管理员"""
        return self.role == 'admin'
    
    def can_create_task(self, SessionLocal, Task):
        """校园版：不限制每人可创建任务数量（仍可能受邮箱绑定/验证等业务规则约束）。"""
        return True


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
    # submission_manage_code removed in campus edition
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
    # 单任务 API 用量（校园版：仅统计，不做限额拦截）
    api_task_get_count = Column(Integer, default=0)  # 轻量 GET（最新 3 条）成功次数
    api_task_all_count = Column(Integer, default=0)  # /all 成功次数
    api_task_all_bytes_total = Column(BigInteger, default=0)  # /all 已下发响应体累计字节
    submission_count_total = Column(Integer, default=0)  # 已接收提交条数（累计）
    submission_bytes_total = Column(BigInteger, default=0)  # 已接收提交 data 的累计字节（UTF-8）
    approver = relationship('User', foreign_keys=[html_approved_by], backref='approved_tasks')
    organization = relationship('Organization', back_populates='tasks')
    shares = relationship('TaskShare', back_populates='task', cascade='all, delete-orphan')
    likes = relationship('TaskLike', back_populates='task', cascade='all, delete-orphan')


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
]
