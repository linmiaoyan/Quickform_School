"""校园版多模态流量/存储限额（任务级 + 账户级，少量规则）。"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

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
    """返回限额配置（MB）；0 表示不限制。"""
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


def task_usage_snapshot(db, task, *, owner_id: Optional[int] = None) -> Dict[str, Any]:
    """任务用量与限额（供任务详情/管理后台展示）。"""
    limits = load_usage_limits()
    api_id = (getattr(task, 'task_id', None) or '').strip()
    att_used = task_attachment_bytes_used(api_id)
    all_bytes = int(getattr(task, 'api_task_all_bytes_total', 0) or 0)
    uid = owner_id if owner_id is not None else getattr(task, 'user_id', None)
    user_att_used = user_attachment_bytes_used(db, uid) if uid else 0

    task_att_cap = _mb_to_bytes(limits['task_attachment_quota_mb'])
    all_cap = _mb_to_bytes(limits['task_api_all_bytes_quota_mb'])
    user_att_cap = _mb_to_bytes(limits['user_attachment_quota_mb'])

    def _pct(used: int, cap: int):
        if cap <= 0:
            return None
        return round(100.0 * used / cap, 1)

    return {
        'limits': limits,
        'attachment_bytes_used': att_used,
        'attachment_quota_bytes': task_att_cap,
        'attachment_pct': _pct(att_used, task_att_cap),
        'api_all_bytes_used': all_bytes,
        'api_all_quota_bytes': all_cap,
        'api_all_pct': _pct(all_bytes, all_cap),
        'user_attachment_bytes_used': user_att_used,
        'user_attachment_quota_bytes': user_att_cap,
        'user_attachment_pct': _pct(user_att_used, user_att_cap),
    }


def check_attachment_upload(db, task, additional_bytes: int) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    上传多模态附件前检查。
    返回 (是否允许, 拒绝原因, 预警文案)。
    """
    additional_bytes = max(0, int(additional_bytes or 0))
    if additional_bytes <= 0:
        return True, None, None

    limits = load_usage_limits()
    api_id = (getattr(task, 'task_id', None) or '').strip()
    task_used = task_attachment_bytes_used(api_id)
    projected_task = task_used + additional_bytes

    task_cap = _mb_to_bytes(limits['task_attachment_quota_mb'])
    if task_cap > 0 and projected_task > task_cap:
        return (
            False,
            f'任务附件存储已达上限（{_format_mb(task_cap)}MB），无法继续上传。请清理旧附件或联系管理员。',
            None,
        )

    user_cap = _mb_to_bytes(limits['user_attachment_quota_mb'])
    if user_cap > 0 and getattr(task, 'user_id', None):
        user_used = user_attachment_bytes_used(db, task.user_id)
        if user_used + additional_bytes > user_cap:
            return (
                False,
                f'账户附件总存储已达上限（{_format_mb(user_cap)}MB），无法继续上传。请清理各任务附件或联系管理员。',
                None,
            )

    warn_parts = []
    if task_cap > 0 and projected_task >= task_cap * WARN_RATIO:
        warn_parts.append(
            f'任务附件已用约 {_format_mb(projected_task)}MB / {_format_mb(task_cap)}MB'
        )
    if user_cap > 0 and getattr(task, 'user_id', None):
        user_projected = user_attachment_bytes_used(db, task.user_id) + additional_bytes
        if user_projected >= user_cap * WARN_RATIO:
            warn_parts.append(
                f'账户附件已用约 {_format_mb(user_projected)}MB / {_format_mb(user_cap)}MB'
            )
    warn = '；'.join(warn_parts) if warn_parts else None
    return True, None, warn


def check_api_all_export(task) -> Tuple[bool, Optional[str], Optional[str]]:
    """GET /api/<id>/all 出站流量检查（累计字节）。"""
    limits = load_usage_limits()
    cap = _mb_to_bytes(limits['task_api_all_bytes_quota_mb'])
    if cap <= 0:
        return True, None, None

    used = int(getattr(task, 'api_task_all_bytes_total', 0) or 0)
    if used >= cap:
        return (
            False,
            f'该任务数据大屏拉取流量已达上限（{_format_mb(cap)}MB），暂无法通过 /all 获取全量数据。请联系任务所有者或管理员。',
            None,
        )
    if used >= cap * WARN_RATIO:
        return (
            True,
            None,
            f'数据大屏拉取流量已用约 {_format_mb(used)}MB / {_format_mb(cap)}MB，接近上限。',
        )
    return True, None, None
