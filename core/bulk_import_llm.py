"""管理员批量导入用户：调用大模型解析非标准 CSV / 表格文本。"""
import json
import logging
import re
from typing import Any, Dict, List

from core.models import AIConfig
from services.ai_service import call_ai_model, get_chat_server_model_light

logger = logging.getLogger(__name__)


def _extract_json_array(text: str) -> List[Any]:
    raw = (text or '').strip()
    if not raw:
        raise ValueError('模型返回为空')
    fence = re.search(r'```(?:json)?\s*([\s\S]*?)```', raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find('[')
    end = raw.rfind(']')
    if start < 0 or end <= start:
        raise ValueError('未找到 JSON 数组')
    return json.loads(raw[start:end + 1])


def _normalize_row(item: Any) -> Dict[str, str]:
    if not isinstance(item, dict):
        raise ValueError('行数据不是对象')
    username = (
        item.get('username') or item.get('用户名') or item.get('user') or item.get('name') or ''
    )
    password = item.get('password') or item.get('密码') or item.get('pwd') or ''
    school = item.get('school') or item.get('学校') or item.get('school_name') or ''
    phone = item.get('phone') or item.get('手机号') or item.get('mobile') or ''
    email = item.get('email') or item.get('邮箱') or ''
    return {
        'username': str(username or '').strip(),
        'password': str(password or '').strip(),
        'school': str(school or '').strip(),
        'phone': str(phone or '').strip(),
        'email': str(email or '').strip(),
    }


def parse_users_csv_with_llm(raw_text: str, *, default_school: str = '') -> List[Dict[str, str]]:
    """将粘贴的 CSV / 表格文本解析为用户行列表（内置用户，非 QFLink）。"""
    text = (raw_text or '').strip()
    if not text:
        raise ValueError('导入内容为空')
    if len(text) > 120_000:
        raise ValueError('导入内容过长（上限约 120KB）')

    default_school_hint = (default_school or '').strip() or '（若原文未给出学校可留空，由系统默认学校填充）'
    prompt = f"""你是数据清洗助手。请从下方文本中抽取用户名单，输出 JSON 数组。
每个元素必须包含字段：username（用户名）、password（密码）、school（学校）。
可选字段：phone（手机号）、email（邮箱）。
规则：
- 仅输出 JSON 数组，不要 markdown 或其它说明。
- 用户名、密码、学校为必填；若某行缺少密码，填 "quickform"。
- 若某行缺少学校且无法从上下文推断，school 可设为 ""（系统默认学校：{default_school_hint}）。
- 用户均为本站「内置用户」（本地账密登录），不是 QFLink 用户。
- 忽略表头说明行、空行、重复用户名（保留第一次出现）。

待解析文本：
{text}
"""
    ai_config = AIConfig(selected_model='chat_server')
    response = call_ai_model(prompt, ai_config, chat_server_model=get_chat_server_model_light())
    data = _extract_json_array(response)
    if not isinstance(data, list):
        raise ValueError('模型返回不是数组')
    rows: List[Dict[str, str]] = []
    for idx, item in enumerate(data, start=1):
        try:
            row = _normalize_row(item)
        except ValueError as e:
            raise ValueError(f'第 {idx} 条解析失败：{e}') from e
        if not row['username']:
            continue
        if not row['password']:
            row['password'] = 'quickform'
        rows.append(row)
    if not rows:
        raise ValueError('未解析到有效用户行')
    return rows
