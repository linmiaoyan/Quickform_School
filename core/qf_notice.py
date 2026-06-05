"""QF 小公告：持久化站内通知（管理员公告 + 系统操作自动推送）。"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func

logger = logging.getLogger(__name__)


def send_qf_notice(db, user_id: int, title: str, body: str, *, kind: str = 'system', event_type: Optional[str] = None):
    """向指定用户写入一条小公告（不 commit）。"""
    from core.models import QFNotice

    title = (title or '').strip()
    body = (body or '').strip()
    if not user_id or not title or not body:
        return None
    notice = QFNotice(
        user_id=int(user_id),
        title=title[:200],
        body=body,
        kind=(kind or 'system')[:20],
        event_type=(event_type or '')[:64] or None,
        is_read=False,
        created_at=datetime.now(),
    )
    db.add(notice)
    return notice


def send_qf_notice_safe(user_id: int, title: str, body: str, *, kind: str = 'system', event_type: Optional[str] = None):
    """独立会话发送，失败仅记日志，不影响主业务事务。"""
    from core.db import SessionLocal

    db = SessionLocal()
    try:
        if not send_qf_notice(db, user_id, title, body, kind=kind, event_type=event_type):
            return
        db.commit()
    except Exception:
        db.rollback()
        logger.exception('发送 QF 小公告失败 user_id=%s event=%s', user_id, event_type)
    finally:
        db.close()


def send_qf_notice_by_username(db, username: str, title: str, body: str, *, kind: str = 'system', event_type: Optional[str] = None):
    """按用户名查找并发送（不 commit）。"""
    from core.models import User

    uname = (username or '').strip()
    if not uname:
        return None
    row = db.query(User.id).filter(func.lower(User.username) == uname.lower()).first()
    if not row:
        return None
    return send_qf_notice(db, row[0], title, body, kind=kind, event_type=event_type)


def send_qf_notice_by_username_safe(username: str, title: str, body: str, *, kind: str = 'system', event_type: Optional[str] = None):
    from core.db import SessionLocal

    db = SessionLocal()
    try:
        notice = send_qf_notice_by_username(db, username, title, body, kind=kind, event_type=event_type)
        if notice:
            db.commit()
    except Exception:
        db.rollback()
        logger.exception('发送 QF 小公告失败 username=%s event=%s', username, event_type)
    finally:
        db.close()


def send_qf_broadcast(db, title: str, body: str, *, exclude_admin: bool = False):
    """向全体用户 fan-out 公告（不 commit）。"""
    from core.models import User

    title = (title or '').strip()
    body = (body or '').strip()
    if not title or not body:
        return 0
    q = db.query(User.id)
    if exclude_admin:
        q = q.filter(User.role != 'admin')
    user_ids = [row[0] for row in q.all()]
    count = 0
    for uid in user_ids:
        if send_qf_notice(db, uid, title, body, kind='announcement', event_type='broadcast'):
            count += 1
    return count


def count_unread_notices(db, user_id: int) -> int:
    from core.models import QFNotice

    if not user_id:
        return 0
    return (
        db.query(func.count(QFNotice.id))
        .filter(QFNotice.user_id == user_id, QFNotice.is_read == False)  # noqa: E712
        .scalar()
        or 0
    )


def list_user_notices(db, user_id: int, *, limit: int = 30) -> List[Dict[str, Any]]:
    from core.models import QFNotice

    rows = (
        db.query(QFNotice)
        .filter(QFNotice.user_id == user_id)
        .order_by(QFNotice.created_at.desc(), QFNotice.id.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    out = []
    for n in rows:
        out.append({
            'id': n.id,
            'title': n.title,
            'body': n.body,
            'kind': n.kind,
            'event_type': n.event_type,
            'is_read': bool(n.is_read),
            'created_at': n.created_at.strftime('%Y-%m-%d %H:%M:%S') if n.created_at else '',
        })
    return out


def mark_notice_read(db, notice_id: int, user_id: int) -> bool:
    from core.models import QFNotice

    n = db.query(QFNotice).filter(QFNotice.id == notice_id, QFNotice.user_id == user_id).first()
    if not n:
        return False
    n.is_read = True
    return True


def mark_all_notices_read(db, user_id: int) -> int:
    from core.models import QFNotice

    return (
        db.query(QFNotice)
        .filter(QFNotice.user_id == user_id, QFNotice.is_read == False)  # noqa: E712
        .update({QFNotice.is_read: True}, synchronize_session=False)
        or 0
    )


# ---------- 系统操作自动通知文案 ----------

def notify_user_registration_approved(username: str):
    send_qf_notice_by_username_safe(
        username,
        '注册审核通过',
        f'您好，{username}！管理员已通过您的注册审核，现在可以正常登录并使用本站。',
        event_type='user_approved',
    )


def notify_user_registration_rejected(username: str, reason: str | None = None, *, user_id: int | None = None):
    reason_text = (reason or '注册审核未通过').strip()
    body = f'您好，{username}。您的注册申请未通过管理员审核，原因：{reason_text}。如有疑问请联系站点管理员。'
    if user_id:
        send_qf_notice_safe(user_id, '注册审核未通过', body, event_type='user_rejected')
    else:
        send_qf_notice_by_username_safe(
            username,
            '注册审核未通过',
            body,
            event_type='user_rejected',
        )


def notify_task_public_approved(user_id: int, task_title: str):
    title = (task_title or '项目').strip() or '项目'
    send_qf_notice_safe(
        user_id,
        '公开申请已通过',
        f'您的项目「{title}」公开申请已通过审核，将展示在灵感广场/项目交流页。',
        event_type='public_approved',
    )


def notify_task_public_rejected(user_id: int, task_title: str):
    title = (task_title or '项目').strip() or '项目'
    send_qf_notice_safe(
        user_id,
        '公开申请未通过',
        f'您的项目「{title}」公开申请未通过审核；可修改后重新申请公开。',
        event_type='public_rejected',
    )


def notify_org_teams_approved(user_id: int, org_name: str):
    name = (org_name or '团队').strip() or '团队'
    send_qf_notice_safe(
        user_id,
        '团队入驻已通过',
        f'您创建的团队「{name}」入驻展示申请已通过，将出现在入驻团队页面。',
        event_type='org_teams_approved',
    )


def notify_org_teams_rejected(user_id: int, org_name: str):
    name = (org_name or '团队').strip() or '团队'
    send_qf_notice_safe(
        user_id,
        '团队入驻未通过',
        f'您创建的团队「{name}」入驻展示申请未通过审核。',
        event_type='org_teams_rejected',
    )


def notify_qflink_disabled(user_id: int, username: str):
    uname = (username or '').strip() or '用户'
    send_qf_notice_safe(
        user_id,
        'QFLink 账号已禁用',
        f'您好，{uname}。管理员已禁用您的 QFLink 登录与相关权限，如有疑问请联系管理员。',
        event_type='qflink_disabled',
    )


def notify_qflink_enabled(user_id: int, username: str):
    uname = (username or '').strip() or '用户'
    send_qf_notice_safe(
        user_id,
        'QFLink 账号已恢复',
        f'您好，{uname}。管理员已恢复您的 QFLink 账号，可重新使用 QFLink 登录。',
        event_type='qflink_enabled',
    )


def notify_qflink_multimodal(user_id: int, username: str, enabled: bool):
    uname = (username or '').strip() or '用户'
    if enabled:
        send_qf_notice_safe(
            user_id,
            '多模态附件已开启',
            f'您好，{uname}。管理员已为您开启多模态附件回收权限。',
            event_type='qflink_multimodal_on',
        )
    else:
        send_qf_notice_safe(
            user_id,
            '多模态附件已关闭',
            f'您好，{uname}。管理员已关闭您的多模态附件回收权限。',
            event_type='qflink_multimodal_off',
        )


def notify_password_reset(user_id: int, username: str):
    uname = (username or '').strip() or '用户'
    send_qf_notice_safe(
        user_id,
        '密码已重置',
        f'您好，{uname}。管理员已将您的登录密码重置为默认密码 123456，请尽快登录后在个人中心修改。',
        event_type='password_reset',
    )
