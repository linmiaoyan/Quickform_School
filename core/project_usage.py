import json
import os
from datetime import datetime, timedelta

from sqlalchemy import func

from .models import Task, Submission, User, ApiAccessLog


def _safe_file_size(path):
    if not path:
        return 0
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
    except Exception:
        return 0
    return 0


def _collect_task_file_bytes(task):
    total = 0
    total += _safe_file_size(getattr(task, 'file_path', None))
    total += _safe_file_size(getattr(task, 'report_file_path', None))

    html_files_raw = getattr(task, 'html_files', None)
    if html_files_raw:
        try:
            arr = json.loads(html_files_raw)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict):
                        total += _safe_file_size(item.get('path'))
        except Exception:
            pass
    return total


def get_top_projects(db, limit=20, sort_by='submissions'):
    limit = max(1, min(int(limit or 20), 500))
    agg_rows = (
        db.query(
            Task.id.label('task_pk'),
            Task.task_id.label('task_id'),
            Task.title.label('task_title'),
            User.username.label('owner_username'),
            func.count(Submission.id).label('submit_count'),
            func.coalesce(func.sum(func.length(Submission.data)), 0).label('submission_bytes'),
        )
        .join(User, User.id == Task.user_id)
        .outerjoin(Submission, Submission.task_id == Task.id)
        .group_by(Task.id, Task.task_id, Task.title, User.username)
        .all()
    )

    now = datetime.utcnow()
    one_hour_ago = now - timedelta(hours=1)
    one_day_ago = now - timedelta(hours=24)

    all_calls_rows = (
        db.query(ApiAccessLog.task_id, func.count(ApiAccessLog.id))
        .filter(ApiAccessLog.endpoint == 'api_task_all', ApiAccessLog.created_at >= one_hour_ago)
        .group_by(ApiAccessLog.task_id)
        .all()
    )
    all_calls_1h = {row[0]: int(row[1] or 0) for row in all_calls_rows}

    submit_rows_24h = (
        db.query(Submission.task_id, func.count(Submission.id))
        .filter(Submission.submitted_at >= one_day_ago)
        .group_by(Submission.task_id)
        .all()
    )
    submissions_24h = {row[0]: int(row[1] or 0) for row in submit_rows_24h}

    result = []
    tasks_cache = {}
    for row in agg_rows:
        task_pk = int(row.task_pk)
        task_obj = tasks_cache.get(task_pk)
        if task_obj is None:
            task_obj = db.get(Task, task_pk)
            tasks_cache[task_pk] = task_obj

        file_bytes = _collect_task_file_bytes(task_obj) if task_obj else 0
        sub_bytes = int(row.submission_bytes or 0)
        total_bytes = file_bytes + sub_bytes
        result.append({
            'task_pk': task_pk,
            'task_id': row.task_id,
            'task_title': row.task_title,
            'owner_username': row.owner_username,
            'submit_count': int(row.submit_count or 0),
            'submission_bytes': sub_bytes,
            'file_bytes': int(file_bytes),
            'total_bytes': int(total_bytes),
            'submission_mb': round(sub_bytes / 1024 / 1024, 2),
            'file_mb': round(file_bytes / 1024 / 1024, 2),
            'total_mb': round(total_bytes / 1024 / 1024, 2),
            'all_calls_1h': int(all_calls_1h.get(task_pk, 0)),
            'submissions_24h': int(submissions_24h.get(task_pk, 0)),
        })

    if sort_by == 'storage':
        result.sort(key=lambda x: (x['total_bytes'], x['submit_count']), reverse=True)
    elif sort_by == 'all_calls':
        result.sort(key=lambda x: (x['all_calls_1h'], x['total_bytes']), reverse=True)
    else:
        result.sort(key=lambda x: (x['submit_count'], x['total_bytes']), reverse=True)

    return result[:limit]


def evaluate_project_alerts(rows, config):
    alerts = []
    for row in rows:
        reasons = []
        if row['all_calls_1h'] >= int(config.get('all_calls_1h', 0)):
            reasons.append(f"/all近1小时调用 {row['all_calls_1h']} 次")
        if row['submissions_24h'] >= int(config.get('submissions_24h', 0)):
            reasons.append(f"近24小时提交 {row['submissions_24h']} 条")
        if row['total_bytes'] >= int(config.get('total_bytes', 0)):
            reasons.append(f"总占用 {row['total_mb']} MB")
        if reasons:
            alerts.append({
                'task_id': row['task_id'],
                'task_title': row['task_title'],
                'owner_username': row['owner_username'],
                'level': 'P1' if row['all_calls_1h'] >= int(config.get('all_calls_1h_p1', 0) or 0) and int(config.get('all_calls_1h_p1', 0) or 0) > 0 else 'P2',
                'reasons': reasons,
            })
    return alerts
