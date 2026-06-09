"""校园版多模态流量/存储限额（任务级 + 账户级，支持单任务/单用户提额）。"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from core.attachment_recovery import list_task_attachment_files

WARN_RATIO = 0.8


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def load_usage_limits() -> Dict[str, int]:
    """返回全局限额配置（MB）；0 表示不限制。"""
    from core.system_config import load_system_config

    cfg = load_system_config()
    return {
        'task_attachment_quota_mb': max(
            0,
            _env_int('TASK_ATTACHMENT_QUOTA_MB', 0)
            or int(getattr(cfg, 'task_attachment_quota_mb', 300) or 0),
        ),
        'task_api_all_bytes_quota_mb': max(
            0,
            _env_int('TASK_API_ALL_BYTES_QUOTA_MB', 0)
            or int(getattr(cfg, 'task_api_all_bytes_quota_mb', 512) or 0),
        ),
        'user_attachment_quota_mb': max(
            0,
            _env_int('USER_ATTACHMENT_QUOTA_MB', 0)
            or int(getattr(cfg, 'user_attachment_quota_mb', 3072) or 0),
        ),
    }


def _resolve_quota_mb(override, system_default: int) -> int:
    """override: None=系统默认，0=不限，正整数=指定 MB。"""
    if override is None:
        return max(0, int(system_default or 0))
    try:
        return max(0, int(override))
    except (TypeError, ValueError):
        return max(0, int(system_default or 0))


def effective_task_attachment_quota_mb(task, user=None) -> int:
    limits = load_usage_limits()
    ov = getattr(task, 'attachment_quota_mb_override', None) if task else None
    return _resolve_quota_mb(ov, limits['task_attachment_quota_mb'])


def effective_task_api_all_quota_mb(task) -> int:
    limits = load_usage_limits()
    ov = getattr(task, 'api_all_bytes_quota_mb_override', None) if task else None
    return _resolve_quota_mb(ov, limits['task_api_all_bytes_quota_mb'])


def effective_user_attachment_quota_mb(user) -> int:
    limits = load_usage_limits()
    ov = getattr(user, 'attachment_quota_mb_override', None) if user else None
    return _resolve_quota_mb(ov, limits['user_attachment_quota_mb'])


def _mb_to_bytes(mb: int) -> int:
    return max(0, int(mb or 0)) * 1024 * 1024


def task_attachment_bytes_used(task_api_id: str) -> int:
    api_id = (task_api_id or '').strip()
    if not api_id:
        return 0
    return sum(int(f.get('size_bytes') or 0) for f in list_task_attachment_files(api_id))


def user_attachment_bytes_used(db, user_id: int) -> int:
    if not user_id:
        return 0
    from core.models import Task

    total = 0
    for row in db.query(Task.task_id).filter(Task.user_id == user_id).all():
        total += task_attachment_bytes_used(row.task_id)
    return total


def _format_mb(bytes_val: int) -> str:
    return f'{bytes_val / 1024 / 1024:.1f}'


def _usage_pct(used: int, cap_bytes: int) -> Optional[float]:
    if cap_bytes <= 0:
        return None
    return round(100.0 * used / cap_bytes, 1)


def _alert_level(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct >= 100:
        return 'exceeded'
    if pct >= WARN_RATIO * 100:
        return 'warning'
    return None


def task_usage_snapshot(db, task, *, owner=None, owner_id: Optional[int] = None) -> Dict[str, Any]:
    """任务用量与生效限额（含提额）。"""
    from core.models import User

    limits = load_usage_limits()
    api_id = (getattr(task, 'task_id', None) or '').strip()
    att_used = task_attachment_bytes_used(api_id)
    all_bytes = int(getattr(task, 'api_task_all_bytes_total', 0) or 0)
    uid = owner_id if owner_id is not None else getattr(task, 'user_id', None)
    if owner is None and uid:
        owner = db.get(User, uid)
    user_att_used = user_attachment_bytes_used(db, uid) if uid else 0

    task_att_mb = effective_task_attachment_quota_mb(task, owner)
    all_mb = effective_task_api_all_quota_mb(task)
    user_att_mb = effective_user_attachment_quota_mb(owner) if owner else limits['user_attachment_quota_mb']

    task_att_cap = _mb_to_bytes(task_att_mb)
    all_cap = _mb_to_bytes(all_mb)
    user_att_cap = _mb_to_bytes(user_att_mb)

    att_pct = _usage_pct(att_used, task_att_cap)
    all_pct = _usage_pct(all_bytes, all_cap)
    user_att_pct = _usage_pct(user_att_used, user_att_cap)

    return {
        'limits': limits,
        'effective_limits_mb': {
            'task_attachment': task_att_mb,
            'task_api_all': all_mb,
            'user_attachment': user_att_mb,
        },
        'overrides': {
            'task_attachment_mb': getattr(task, 'attachment_quota_mb_override', None),
            'task_api_all_mb': getattr(task, 'api_all_bytes_quota_mb_override', None),
            'user_attachment_mb': getattr(owner, 'attachment_quota_mb_override', None) if owner else None,
        },
        'attachment_bytes_used': att_used,
        'attachment_quota_bytes': task_att_cap,
        'attachment_pct': att_pct,
        'api_all_bytes_used': all_bytes,
        'api_all_quota_bytes': all_cap,
        'api_all_pct': all_pct,
        'user_attachment_bytes_used': user_att_used,
        'user_attachment_quota_bytes': user_att_cap,
        'user_attachment_pct': user_att_pct,
    }


def check_attachment_upload(db, task, additional_bytes: int) -> Tuple[bool, Optional[str], Optional[str]]:
    additional_bytes = max(0, int(additional_bytes or 0))
    if additional_bytes <= 0:
        return True, None, None

    from core.models import User

    owner = db.get(User, task.user_id) if getattr(task, 'user_id', None) else None
    api_id = (getattr(task, 'task_id', None) or '').strip()
    task_used = task_attachment_bytes_used(api_id)
    projected_task = task_used + additional_bytes

    task_cap = _mb_to_bytes(effective_task_attachment_quota_mb(task, owner))
    if task_cap > 0 and projected_task > task_cap:
        return (
            False,
            f'任务附件存储已达上限（{_format_mb(task_cap)}MB），无法继续上传。请清理旧附件或联系管理员提额。',
            None,
        )

    user_cap = _mb_to_bytes(effective_user_attachment_quota_mb(owner)) if owner else 0
    if user_cap > 0 and owner:
        user_used = user_attachment_bytes_used(db, owner.id)
        if user_used + additional_bytes > user_cap:
            return (
                False,
                f'账户附件总存储已达上限（{_format_mb(user_cap)}MB），无法继续上传。请清理各任务附件或联系管理员提额。',
                None,
            )

    warn_parts = []
    if task_cap > 0 and projected_task >= task_cap * WARN_RATIO:
        warn_parts.append(
            f'任务附件已用约 {_format_mb(projected_task)}MB / {_format_mb(task_cap)}MB'
        )
    if user_cap > 0 and owner:
        user_projected = user_attachment_bytes_used(db, owner.id) + additional_bytes
        if user_projected >= user_cap * WARN_RATIO:
            warn_parts.append(
                f'账户附件已用约 {_format_mb(user_projected)}MB / {_format_mb(user_cap)}MB'
            )
    warn = '；'.join(warn_parts) if warn_parts else None
    return True, None, warn


def check_api_all_export(task, db=None) -> Tuple[bool, Optional[str], Optional[str]]:
    cap = _mb_to_bytes(effective_task_api_all_quota_mb(task))
    if cap <= 0:
        return True, None, None

    used = int(getattr(task, 'api_task_all_bytes_total', 0) or 0)
    if used >= cap:
        return (
            False,
            f'该任务数据大屏拉取流量已达上限（{_format_mb(cap)}MB），暂无法通过 /all 获取全量数据。请联系任务所有者或管理员提额。',
            None,
        )
    if used >= cap * WARN_RATIO:
        return (
            True,
            None,
            f'数据大屏拉取流量已用约 {_format_mb(used)}MB / {_format_mb(cap)}MB，接近上限。',
        )
    return True, None, None


def collect_quota_alerts(db, *, task_limit: int = 300, user_limit: int = 200) -> Dict[str, Any]:
    """汇总管理端预警：任务附件、任务 /all、用户附件合计。"""
    from core.models import Task, User

    global_limits = load_usage_limits()
    task_rows: List[Dict[str, Any]] = []
    user_rows: List[Dict[str, Any]] = []
    seen_users = set()

    tasks = (
        db.query(Task)
        .order_by(Task.created_at.desc())
        .limit(max(1, min(task_limit, 1000)))
        .all()
    )
    for task in tasks:
        owner = db.get(User, task.user_id) if task.user_id else None
        snap = task_usage_snapshot(db, task, owner=owner)
        title = (task.title or '').strip() or f'任务#{task.id}'
        author = (owner.username if owner else '') or '—'

        if snap['attachment_quota_bytes'] > 0:
            lvl = _alert_level(snap['attachment_pct'])
            if lvl:
                task_rows.append({
                    'kind': 'task_attachment',
                    'level': lvl,
                    'task_id': task.id,
                    'api_id': task.task_id,
                    'title': title,
                    'author': author,
                    'used_mb': round(snap['attachment_bytes_used'] / 1024 / 1024, 2),
                    'quota_mb': snap['effective_limits_mb']['task_attachment'],
                    'pct': snap['attachment_pct'],
                    'override_mb': snap['overrides']['task_attachment_mb'],
                })

        if snap['api_all_quota_bytes'] > 0:
            lvl = _alert_level(snap['api_all_pct'])
            if lvl:
                task_rows.append({
                    'kind': 'task_api_all',
                    'level': lvl,
                    'task_id': task.id,
                    'api_id': task.task_id,
                    'title': title,
                    'author': author,
                    'used_mb': round(snap['api_all_bytes_used'] / 1024 / 1024, 2),
                    'quota_mb': snap['effective_limits_mb']['task_api_all'],
                    'pct': snap['api_all_pct'],
                    'override_mb': snap['overrides']['task_api_all_mb'],
                })

        if owner and owner.id not in seen_users:
            seen_users.add(owner.id)
            u_cap = snap['user_attachment_quota_bytes']
            if u_cap > 0:
                lvl = _alert_level(snap['user_attachment_pct'])
                if lvl:
                    user_rows.append({
                        'kind': 'user_attachment',
                        'level': lvl,
                        'user_id': owner.id,
                        'username': owner.username,
                        'used_mb': round(snap['user_attachment_bytes_used'] / 1024 / 1024, 2),
                        'quota_mb': snap['effective_limits_mb']['user_attachment'],
                        'pct': snap['user_attachment_pct'],
                        'override_mb': snap['overrides']['user_attachment_mb'],
                    })

    if len(seen_users) < user_limit:
        extra_users = (
            db.query(User)
            .filter(User.role != 'admin')
            .order_by(User.created_at.desc())
            .limit(user_limit)
            .all()
        )
        for u in extra_users:
            if u.id in seen_users:
                continue
            seen_users.add(u.id)
            cap_mb = effective_user_attachment_quota_mb(u)
            if cap_mb <= 0:
                continue
            used = user_attachment_bytes_used(db, u.id)
            pct = _usage_pct(used, _mb_to_bytes(cap_mb))
            lvl = _alert_level(pct)
            if lvl:
                user_rows.append({
                    'kind': 'user_attachment',
                    'level': lvl,
                    'user_id': u.id,
                    'username': u.username,
                    'used_mb': round(used / 1024 / 1024, 2),
                    'quota_mb': cap_mb,
                    'pct': pct,
                    'override_mb': getattr(u, 'attachment_quota_mb_override', None),
                })

    def _sort_key(row):
        return (0 if row['level'] == 'exceeded' else 1, -(row.get('pct') or 0))

    task_rows.sort(key=_sort_key)
    user_rows.sort(key=_sort_key)

    exceeded = sum(1 for r in task_rows + user_rows if r['level'] == 'exceeded')
    warning = sum(1 for r in task_rows + user_rows if r['level'] == 'warning')

    return {
        'global_limits': global_limits,
        'task_alerts': task_rows,
        'user_alerts': user_rows,
        'exceeded_count': exceeded,
        'warning_count': warning,
        'total_alert_count': exceeded + warning,
    }


def parse_quota_override_form(raw) -> Optional[int]:
    """解析提额表单：空=清除覆盖(NULL)，0=不限，正整数=MB。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == '':
        return None
    try:
        return max(0, min(102400, int(s)))
    except (TypeError, ValueError):
        raise ValueError('限额须为 0～102400 的整数（MB）')
