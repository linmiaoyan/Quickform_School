"""数据库模型定义和迁移"""
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, inspect, text, UniqueConstraint
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
    email = Column(String(100), unique=True, nullable=False)
    password = Column(String(200), nullable=False)
    school = Column(String(200))
    phone = Column(String(20))
    role = Column(String(20), default='user')
    task_limit = Column(Integer, default=3)  # 任务创建上限，-1表示无限制
    email_verified = Column(Boolean, default=False)  # 创建第二个任务前需验证邮箱。已有数据库需执行：ALTER TABLE user ADD COLUMN email_verified BOOLEAN DEFAULT 1;
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


class AIConfig(Base):
    __tablename__ = 'ai_config'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id'), unique=True)
    user = relationship('User', back_populates='ai_config')
    selected_model = Column(String(50), default='deepseek')
    deepseek_api_key = Column(String(200))
    doubao_api_key = Column(String(200))
    doubao_secret_key = Column(String(200))
    qwen_api_key = Column(String(200))
    # 硅基流动（ChatServer）配置
    chat_server_api_url = Column(String(200))
    chat_server_api_token = Column(String(200))
    # 更多模型
    moonshot_api_key = Column(String(200))
    glm_api_key = Column(String(200))
    ernie_api_key = Column(String(200))
    ernie_secret_key = Column(String(200))
    openrouter_api_key = Column(String(200))


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
    can_edit = Column(Boolean, default=True)  # 是否可编辑
    
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


def migrate_database(engine):
    """数据库迁移函数"""
    try:
        inspector = inspect(engine)
        columns = [col['name'] for col in inspector.get_columns('user')]
        ai_cfg_cols = [col['name'] for col in inspector.get_columns('ai_config')] if 'ai_config' in inspector.get_table_names() else []
        task_cols = [col['name'] for col in inspector.get_columns('task')] if 'task' in inspector.get_table_names() else []
        cert_req_cols = [col['name'] for col in inspector.get_columns('certification_request')] if 'certification_request' in inspector.get_table_names() else []
        
        with engine.begin() as conn:
            if 'school' not in columns:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN school VARCHAR(200)"))
                    logger.info("成功添加school字段到user表")
                except Exception as e:
                    logger.warning(f"添加school字段失败（可能已存在）: {str(e)}")
            
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
            
            # ai_config 新增更多模型字段
            for col_name in ['moonshot_api_key', 'glm_api_key', 'ernie_api_key', 'ernie_secret_key', 'openrouter_api_key']:
                if ai_cfg_cols and col_name not in ai_cfg_cols:
                    try:
                        conn.execute(text(f"ALTER TABLE ai_config ADD COLUMN {col_name} VARCHAR(200)"))
                        logger.info(f"成功为ai_config添加{col_name}")
                    except Exception as e:
                        logger.warning(f"添加{col_name}失败（可能已存在）: {str(e)}")
    except Exception as e:
        logger.error(f"数据库迁移失败: {str(e)}")
