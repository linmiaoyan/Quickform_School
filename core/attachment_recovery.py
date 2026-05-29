"""多模态/API 附件磁盘文件列举与回收（管理员）。"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Set, Tuple

from .models import Submission, Task

# 与 blueprint 中目录约定一致
_QUICKFORM_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.abspath(os.path.join(_QUICKFORM_DIR, '..'))
UPLOAD_FOLDER = os.path.join(_QUICKFORM_DIR, 'uploads')
STATIC_UPLOADS = os.path.abspath(os.path.join(_APP_ROOT, 'static', 'uploads'))


def multimodal_upload_dir(task_api_id: str) -> str:
    return os.path.join(STATIC_UPLOADS, str(task_api_id))


def legacy_api_files_dir(task_api_id: str) -> str:
    return os.path.join(UPLOAD_FOLDER, 'api_files', str(task_api_id))


def _safe_list_files(base_dir: str, url_prefix: str, storage: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not base_dir or not os.path.isdir(base_dir):
        return out
    try:
        for name in sorted(os.listdir(base_dir)):
            if name.startswith('.'):
                continue
            full = os.path.join(base_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            out.append({
                'storage': storage,
                'name': name,
                'path_key': f'{storage}:{name}',
                'size_bytes': size,
                'url': f'{url_prefix.rstrip("/")}/{name}',
            })
    except OSError:
        pass
    return out


def list_task_attachment_files(task_api_id: str) -> List[Dict[str, Any]]:
    """列出任务下多模态目录与迁移 api_files 目录中的文件。"""
    api_id = (task_api_id or '').strip()
    if not api_id:
        return []
    files: List[Dict[str, Any]] = []
    files.extend(_safe_list_files(
        multimodal_upload_dir(api_id),
        f'/static/uploads/{api_id}',
        'multimodal',
    ))
    files.extend(_safe_list_files(
        legacy_api_files_dir(api_id),
        f'/uploads/api_files/{api_id}',
        'api_files',
    ))
    return files


def _collect_paths_from_submission_data(raw: str, refs: Set[str]) -> None:
    if not raw:
        return
    text = raw if isinstance(raw, str) else str(raw)
    for m in re.finditer(r'/static/uploads/[0-9a-zA-Z_-]+/[^"\'\s<>]+', text):
        refs.add(m.group(0).split('?')[0])
    for m in re.finditer(r'/uploads/api_files/[0-9a-zA-Z_-]+/[^"\'\s<>]+', text):
        refs.add(m.group(0).split('?')[0])
    try:
        data = json.loads(text)
    except Exception:
        return

    def walk(obj):
        if isinstance(obj, str):
            if '/static/uploads/' in obj or '/uploads/api_files/' in obj:
                refs.add(obj.split('?')[0])
        elif isinstance(obj, list):
            for x in obj:
                walk(x)
        elif isinstance(obj, dict):
            for x in obj.values():
                walk(x)

    walk(data)


def referenced_attachment_urls(db, task_pk: int) -> Set[str]:
    refs: Set[str] = set()
    rows = db.query(Submission.data).filter(Submission.task_id == task_pk).all()
    for (raw,) in rows:
        _collect_paths_from_submission_data(raw or '', refs)
    return refs


def annotate_attachment_references(files: List[Dict[str, Any]], referenced: Set[str]) -> List[Dict[str, Any]]:
    out = []
    for f in files:
        row = dict(f)
        url = (row.get('url') or '').split('?')[0]
        row['referenced'] = url in referenced
        out.append(row)
    return out


def resolve_attachment_file_path(task_api_id: str, storage: str, filename: str) -> str | None:
    api_id = (task_api_id or '').strip()
    name = (filename or '').strip()
    if not api_id or not name or '/' in name or '\\' in name or '..' in name:
        return None
    if storage == 'multimodal':
        base = multimodal_upload_dir(api_id)
    elif storage == 'api_files':
        base = legacy_api_files_dir(api_id)
    else:
        return None
    full = os.path.join(base, name)
    base_real = os.path.realpath(base)
    try:
        full_real = os.path.realpath(full)
    except OSError:
        return None
    if not full_real.startswith(base_real + os.sep) and full_real != base_real:
        return None
    if os.path.isfile(full_real):
        return full_real
    return None


def delete_attachment_file(task_api_id: str, storage: str, filename: str) -> Tuple[bool, str]:
    path = resolve_attachment_file_path(task_api_id, storage, filename)
    if not path:
        return False, '文件不存在或路径非法'
    try:
        os.remove(path)
        return True, '已删除'
    except OSError as e:
        return False, str(e)


def task_attachment_summary(db, task: Task) -> Dict[str, Any]:
    api_id = (task.task_id or '').strip()
    files = list_task_attachment_files(api_id)
    refs = referenced_attachment_urls(db, task.id)
    files = annotate_attachment_references(files, refs)
    total_bytes = sum(int(f.get('size_bytes') or 0) for f in files)
    orphan = sum(1 for f in files if not f.get('referenced'))
    return {
        'task_id': task.id,
        'api_id': api_id,
        'title': task.title or '',
        'file_count': len(files),
        'orphan_count': orphan,
        'total_bytes': total_bytes,
        'files': files,
    }
