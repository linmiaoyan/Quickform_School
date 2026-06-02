"""数据收集 API（POST /api/<task_id>）的限额与统一错误响应（校园版）。"""
import json
import os

from flask import jsonify

# 单条 submission.data 建议上限（与数据库 TEXT 及业务提示一致）
API_MAX_JSON_FIELD_KB = max(16, int(os.getenv('API_MAX_JSON_FIELD_KB', '60') or '60'))


def api_max_file_size_mb() -> int:
    return max(1, int(os.getenv('API_MAX_FILE_SIZE_MB', '20') or '20'))


def api_max_request_body_bytes() -> int:
    mb = max(1, int(os.getenv('MAX_REQUEST_BODY_MB', '10') or '10'))
    return mb * 1024 * 1024


def api_max_json_field_bytes() -> int:
    return API_MAX_JSON_FIELD_KB * 1024


def api_allowed_extensions_csv() -> str:
    return (
        os.getenv(
            'API_ALLOWED_FILE_EXTENSIONS',
            'jpg,jpeg,png,gif,webp,wav,mp3,webm,mp4,txt,pdf,doc,docx,html,htm,xls,xlsx,zip',
        )
        or 'jpg,jpeg,png,gif,webp,wav,mp3,webm,mp4,txt,pdf,doc,docx,html,htm,xls,xlsx,zip'
    )


def api_limits_for_client() -> dict:
    """注入学生端 HTML 的限额说明（与服务器配置一致）。"""
    return {
        'maxFileMb': api_max_file_size_mb(),
        'maxBodyMb': api_max_request_body_bytes() // (1024 * 1024),
        'maxJsonKb': API_MAX_JSON_FIELD_KB,
        'allowedExtensions': [
            e.strip().lower()
            for e in api_allowed_extensions_csv().split(',')
            if e.strip()
        ],
    }


def attach_submit_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-QuickForm-Device-ID, X-Device-ID'
    response.headers['Cache-Control'] = 'no-store'
    return response


def submit_api_json_response(error: str, message: str, status: int = 400, **extra):
    """对外收集接口统一 JSON：含 error 机器码 + message 人类可读（UTF-8，非 ASCII 转义）。"""
    payload = {'error': error, 'message': message}
    for k, v in extra.items():
        if v is not None:
            payload[k] = v
    response = jsonify(payload)
    return attach_submit_cors_headers(response), status


def inject_student_page_scripts(html_content: str, base_url: str) -> str:
    """在学生端 HTML 注入限额 meta 与提交增强脚本（多模态 / 错误解析）。"""
    import html as html_module

    limits_json = html_module.escape(
        json.dumps(api_limits_for_client(), ensure_ascii=False),
        quote=True,
    )
    meta = f'<meta name="qf-api-limits" content="{limits_json}">'
    scripts = (
        f'<script src="{base_url}/static/js/form-enhancements.js" defer></script>\n'
        f'<script src="{base_url}/static/js/qf-api-submit.js" defer></script>'
    )
    block = meta + '\n' + scripts
    if '</head>' in html_content:
        return html_content.replace('</head>', block + '\n</head>', 1)
    if '</body>' in html_content:
        return html_content.replace('</body>', block + '\n</body>', 1)
    return html_content + '\n' + block + '\n'
