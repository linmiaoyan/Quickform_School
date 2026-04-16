"""AI服务 - 处理AI模型调用和分析相关功能"""
import json
import re
import requests
import threading
import logging
import os
from datetime import datetime
from collections import Counter
from flask import current_app
from core.secret_store import decrypt_ai_config_inplace

logger = logging.getLogger(__name__)

# 分析参数默认固定在代码中（不依赖 .env）
PROMPT_MAX_CHARS = 120000
DEEPSEEK_MAX_TOKENS = 8192
DOUBAO_MAX_TOKENS = 32768
QWEN_MAX_TOKENS = 32768
# 硅基流动等平台校验：prompt token + max_tokens 不得超过 max_seq_len（如 32K），
# 不能将 max_tokens 设为上下文全长；输出与输入共享同一上限。
CHAT_SERVER_MAX_TOKENS = 8192
CHAT_SERVER_MAX_SEQ_LEN = 32768
CHAT_SERVER_SEQ_MARGIN = 256
CHAT_SERVER_MIN_MAX_TOKENS = 256
# 为截断用户正文预留的「输出侧」字数账（与 CHAT_SERVER_MAX_TOKENS 同量级即可）
CHAT_SERVER_TRUNC_RESERVE_OUTPUT = 8192
# 注入分析提示词的 HTML 预分析全文上限（与提交条数无关，避免单段过长占满上下文）
HTML_ANALYSIS_IN_PROMPT_MAX_CHARS = 12000
COMPACT_MODE_ENABLED = True
COMPACT_FIELD_MAX_LEN = 180
REPEAT_FILTER_ENABLED = True
REPEAT_MIN_SAMPLES = 20
REPEAT_MAX_UNIQUE_RATIO = 0.05
REPEAT_SHOW_TOPN = 8
NOISE_KEYS = {
    'timestamp', 'time', 'local_time', 'created_at', 'updated_at', 'submit_time',
    'request_id', 'trace_id', 'session_id', 'device_id', 'ip', 'user_agent'
}
KEEP_KEYS = {
    'title', 'teacher', 'comment', 'review', 'ratings', 'score', 'grade',
    'lesson_index', 'class', 'subject', 'name'
}


def _collapse_extra_newlines(text):
    """将连续多个换行压成单个换行，减少提示词中的多余空行。"""
    if not text:
        return text
    t = text.replace('\r\n', '\n').replace('\r', '\n')
    return re.sub(r'\n{2,}', '\n', t)


def _normalize_submission_payload(parsed):
    """
    将单条提交的 JSON 解析结果规范为 dict，便于统一按 .items() 遍历。
    部分接口/测试会直接提交数字、字符串或数组等标量/非对象 JSON。
    """
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        return {'_root_list': parsed}
    return {'_root_value': parsed}


def _chat_server_fit_user_and_max_tokens(system_text, user_text):
    """
    硅基流动等接口要求：估计输入 token + max_tokens <= max_seq_len。
    在 max_tokens 取上限前，先截断过长 user，并返回动态 max_tokens，避免 HTTP 400（如 code 20015）。
    """
    sys_t = system_text or ''
    usr_t = user_text or ''
    max_seq = CHAT_SERVER_MAX_SEQ_LEN
    margin = CHAT_SERVER_SEQ_MARGIN
    min_out = CHAT_SERVER_MIN_MAX_TOKENS
    overhead = 192

    def est_in(s, u):
        return max(64, (len(s) + len(u) + 1) // 2 + overhead)

    reserve_out = min(CHAT_SERVER_MAX_TOKENS, CHAT_SERVER_TRUNC_RESERVE_OUTPUT)
    max_est_in = max_seq - margin - reserve_out
    max_user_chars = max(512, 2 * max(0, max_est_in - overhead) - len(sys_t) - 1)
    notice = '\n【系统提示】为适配模型上下文上限，已截断部分输入。'

    if len(usr_t) > max_user_chars:
        cut = max(0, max_user_chars - len(notice))
        usr_t = usr_t[:cut] + notice
        logger.info(
            '硅基流动：已按上下文上限截断用户提示，约 %d 字符（max_seq_len=%d）',
            len(usr_t),
            max_seq,
        )

    for _ in range(48):
        est = est_in(sys_t, usr_t)
        if est + min_out + margin <= max_seq:
            break
        if len(usr_t) <= 120:
            usr_t = notice.strip() or '…'
            break
        usr_t = usr_t[: max(120, len(usr_t) * 3 // 4)]
    if not usr_t.strip():
        usr_t = notice.strip() or '…'

    est = est_in(sys_t, usr_t)
    room = max_seq - margin - est
    # 平台按「输入 + max_tokens」校验，room 为剩余额度；不足时降到 1，避免再次 400
    max_tokens = max(1, min(CHAT_SERVER_MAX_TOKENS, room))
    return usr_t, max_tokens


def _clip_prompt(prompt):
    """
    控制输入提示词长度，避免超大数据导致上游 400/500/超时。
    默认上限 120000 字符。
    """
    max_chars = PROMPT_MAX_CHARS
    if max_chars <= 0 or len(prompt) <= max_chars:
        return prompt
    omitted = len(prompt) - max_chars
    notice = (
        "\n\n【系统提示】由于数据量过大，已自动截断部分输入内容。"
        f"本次省略约 {omitted} 个字符；如需更完整分析，请缩小数据范围后重试。\n"
    )
    clipped = prompt[:max_chars]
    # 尽量把提示信息放在尾部，帮助模型理解是“截断后的数据”
    if len(clipped) > len(notice):
        clipped = clipped[:-len(notice)] + notice
    return clipped


def _normalize_spaces(text):
    """压缩多余空白，降低 token 占用。"""
    if text is None:
        return ''
    s = str(text).replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    return ' '.join(s.split())


def _compact_field_value(value, max_text_len=180):
    """
    字段值紧凑化：
    - dict/list: 紧凑 JSON（去空格）
    - str: 压缩空白并截断
    - 其他: 转字符串并压缩空白
    """
    if isinstance(value, (dict, list)):
        try:
            txt = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        except Exception:
            txt = _normalize_spaces(str(value))
    else:
        txt = _normalize_spaces(str(value))
    if len(txt) > max_text_len:
        return txt[:max_text_len] + f"...(截断{len(txt)-max_text_len}字)"
    return txt


def _get_compact_filter_sets():
    """
    返回噪声字段集合与强保留字段集合。
    - NOISE: 默认弱化的字段（时间戳/追踪ID等）
    - KEEP: 默认强保留字段（评价/分数/学科等）
    """
    return NOISE_KEYS, KEEP_KEYS


def _should_skip_field(field_name, compact_mode, noise_set, keep_set):
    """紧凑模式下，按字段名判断是否可跳过。"""
    if not compact_mode:
        return False
    key = (field_name or '').strip().lower()
    if not key:
        return False
    if key in keep_set:
        return False
    return key in noise_set


def _build_high_repeat_field_set(all_data, compact_mode, keep_set):
    """
    识别高重复低信息字段（默认开启）：
    - 在样本中出现次数 >= 20
    - 去重率 <= 5%
    - 且不在 keep_set
    """
    if not compact_mode:
        return set()
    if not REPEAT_FILTER_ENABLED:
        return set()
    min_samples = REPEAT_MIN_SAMPLES
    max_unique_ratio = REPEAT_MAX_UNIQUE_RATIO

    field_values = {}
    for row in all_data:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            key = (k or '').strip().lower()
            if not key or key in keep_set:
                continue
            field_values.setdefault(key, []).append(_compact_field_value(v, max_text_len=80))

    repeated = set()
    for key, vals in field_values.items():
        n = len(vals)
        if n < min_samples:
            continue
        uniq = len(set(vals))
        ratio = uniq / max(n, 1)
        if ratio <= max_unique_ratio:
            repeated.add(key)
    return repeated


def _parse_deepseek_error(response):
    """从 DeepSeek API 错误响应中提取可读信息，便于排查 400 等错误"""
    try:
        body = (response.text or "").strip()
        if not body:
            return response.reason or "无响应内容"
        data = json.loads(body)
        # 常见结构: {"error": {"message": "..."}} 或 {"message": "..."}
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return err["message"]
        if data.get("message"):
            return data["message"]
        return body[:400]
    except Exception:
        return (response.text or response.reason or "未知错误")[:400]


def _chat_server_model_resolve(env_key, config_key, code_fallback):
    """硅基流动模型 ID：优先环境变量，其次 Flask app.config（无请求上下文时忽略）。"""
    v = (os.environ.get(env_key) or '').strip()
    if v:
        return v
    try:
        v = (current_app.config.get(config_key) or '').strip()
        if v:
            return v
    except RuntimeError:
        pass
    return code_fallback


def get_chat_server_model_default():
    """硅基流动：主路径（一键生成、智能分析报告主体、AI 改 HTML、连通性测试等）。"""
    return _chat_server_model_resolve(
        'CHAT_SERVER_MODEL', 'CHAT_SERVER_MODEL',
        'Pro/deepseek-ai/DeepSeek-V3.2',
    )


def get_chat_server_model_light():
    """硅基流动：轻量路径（上传后 HTML 摘要分析、智能分析页「仅润色提示词」步骤）。"""
    return _chat_server_model_resolve(
        'CHAT_SERVER_MODEL_LIGHT', 'CHAT_SERVER_MODEL_LIGHT',
        'deepseek-ai/DeepSeek-V3.2',
    )


def call_ai_model(prompt, ai_config, *, chat_server_model=None):
    """调用 AI 模型生成文本。

    上游欠费、限流、鉴权失败等会以 ``Exception`` 抛出，由调用方（路由或后台线程）捕获处理；
    正常情况下**不会**因此终止 Flask/Waitress 进程；主应用在 ``app.py`` 还对未捕获异常做了统一 500 兜底。

    chat_server_model: 仅当 ``selected_model == 'chat_server'`` 时生效；非空则作为硅基流动 ``model`` 字段，否则用
    :func:`get_chat_server_model_default`。
    """
    decrypt_ai_config_inplace(ai_config)
    prompt = _clip_prompt(prompt)
    if ai_config.selected_model == 'deepseek':
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ai_config.deepseek_api_key}"
        }
        data = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你面向的用户一般是教师和学生"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": min(DEEPSEEK_MAX_TOKENS, 8192)   # DeepSeek 当前有效范围为 [1, 8192]
        }
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=240)
            if not response.ok:
                _detail = _parse_deepseek_error(response)
                msg = f"DeepSeek API调用失败: {response.status_code} — {_detail}"
                logger.error(msg)
                raise Exception(msg)
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                _detail = _parse_deepseek_error(e.response)
                msg = f"DeepSeek API调用失败: {e.response.status_code} — {_detail}"
            else:
                msg = f"DeepSeek API调用失败: {str(e)}"
            logger.error(msg)
            raise Exception(msg)
        except Exception as e:
            if "DeepSeek API" in str(e):
                raise
            logger.error(f"DeepSeek API调用失败: {str(e)}")
            raise Exception(f"DeepSeek API调用失败: {str(e)}")
    
    elif ai_config.selected_model == 'doubao':
        url = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ai_config.doubao_api_key}"
        }
        data = {
            "model": "doubao-seed-1-6-251015",
            "messages": [
                {"role": "system", "content": "你面向的用户一般是教师和学生"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": DOUBAO_MAX_TOKENS
        }
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=240)
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"豆包API调用失败: {str(e)}")
            raise Exception(f"豆包API调用失败: {str(e)}")
    
    elif ai_config.selected_model == 'qwen':
        url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ai_config.qwen_api_key}"
        }
        data = {
            "model": "qwen-plus",
            "input": {
                "messages": [
                    {"role": "system", "content": "你面向的用户一般是教师和学生"},
                    {"role": "user", "content": prompt}
                ]
            },
            "parameters": {
                "temperature": 0.7,
                "max_tokens": QWEN_MAX_TOKENS
            }
        }
        
        try:
            logger.info(f"调用阿里云百炼API，模型: qwen-plus")
            response = requests.post(url, headers=headers, json=data, timeout=240)
            
            if response.status_code != 200:
                raise Exception(f"阿里云百炼API调用失败，状态码: {response.status_code}，响应: {response.text[:200]}")
            
            if not response.text:
                raise Exception("阿里云百炼API返回空响应")
            
            try:
                result = response.json()
            except ValueError as ve:
                raise Exception(f"阿里云百炼API返回非JSON响应: {response.text[:200]}")
            
            if isinstance(result, dict) and "code" in result and result["code"] != "200":
                raise Exception(f"阿里云百炼API调用失败: {result.get('message', '未知错误')} (错误码: {result.get('code')})")
            
            if isinstance(result, dict):
                if "output" in result and "text" in result["output"]:
                    return result["output"]["text"]
                elif "choices" in result and len(result["choices"]) > 0:
                    choice = result["choices"][0]
                    if "message" in choice and "content" in choice["message"]:
                        return choice["message"]["content"]
                    elif "text" in choice:
                        return choice["text"]
                elif "data" in result and "choices" in result["data"] and len(result["data"]["choices"]) > 0:
                    choice = result["data"]["choices"][0]
                    if "message" in choice and "content" in choice["message"]:
                        return choice["message"]["content"]
            
            raise Exception(f"阿里云百炼API返回未知格式的响应: {str(result)[:200]}")
        except requests.exceptions.RequestException as re:
            logger.error(f"阿里云百炼API网络请求异常: {str(re)}")
            raise Exception(f"阿里云百炼API网络请求异常: {str(re)}")
        except Exception as e:
            logger.error(f"阿里云百炼API调用失败: {str(e)}")
            raise Exception(f"阿里云百炼API调用失败: {str(e)}")
    
    elif ai_config.selected_model == 'chat_server':
        # 直接通过HTTP请求硅基流动 OpenAI 兼容接口（避免SDK依赖）
        import os as _os
        import requests as _requests
        # Token 解析顺序：用户配置 > 环境变量 > 应用配置
        api_key = (ai_config.chat_server_api_token or '').strip()
        if not api_key:
            api_key = (_os.environ.get('CHAT_SERVER_API_TOKEN', '') or '').strip()
        if not api_key:
            try:
                api_key = (current_app.config.get('CHAT_SERVER_API_TOKEN', '') or '').strip()
            except RuntimeError:
                api_key = ''
        if not api_key:
            raise Exception('硅基流动未配置，请设置 CHAT_SERVER_API_TOKEN 或在配置页填写 Token')
        model_id = (chat_server_model or '').strip() or get_chat_server_model_default()
        system_msg = '你面向的用户一般是教师和学生'
        user_msg, sf_max_tokens = _chat_server_fit_user_and_max_tokens(system_msg, prompt)
        url = 'https://api.siliconflow.cn/v1/chat/completions'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
        # Pro/ 前缀模型失败时回退到同一路径的非 Pro 版本（如 Pro/deepseek-ai/DeepSeek-V3.2 -> deepseek-ai/DeepSeek-V3.2）
        models_try = [model_id]
        if model_id.startswith('Pro/') and len(model_id) > len('Pro/'):
            _alt = model_id[4:].strip()
            if _alt and _alt != model_id:
                models_try.append(_alt)

        last_error = None
        for _mi, _mid in enumerate(models_try):
            payload = {
                'model': _mid,
                'messages': messages,
                'max_tokens': sf_max_tokens,
            }
            try:
                resp = _requests.post(url, headers=headers, json=payload, timeout=(10, 240))
            except _requests.Timeout as e:
                last_error = e
                if _mi + 1 < len(models_try):
                    logger.warning(
                        '硅基流动 model=%s 超时，回退重试 model=%s',
                        _mid,
                        models_try[_mi + 1],
                    )
                    continue
                logger.error(f"硅基流动超时: {e}")
                raise Exception(f"硅基流动超时: {e}")

            if resp.status_code != 200:
                raw = (resp.text or "").strip()
                detail = raw[:400]
                if resp.status_code == 401:
                    hint = "（恢复默认时使用的是系统/环境变量中的 Token，若无效请在个人中心选择其他模型或填写自己的硅基流动 Token）"
                    raise Exception(f"当前使用：硅基流动。Token 无效或已过期 {hint} — {detail[:200]}")
                if resp.status_code == 402:
                    raise Exception(
                        "HTTP 402: 硅基流动账户余额或额度不足（Payment Required）。"
                        "请至硅基流动控制台充值，或在个人中心填写您自己的 API Token；"
                        "平台默认 Key 用尽时仅影响依赖该 Key 的 AI 调用，站点其它功能仍可正常使用。"
                        f" 详情：{detail[:200]}"
                    )
                if resp.status_code == 429:
                    raise Exception(
                        f"HTTP 429: 硅基流动请求过于频繁或被限流，请稍后再试。{detail[:180]}"
                    )
                if resp.status_code == 400 and raw:
                    try:
                        errj = json.loads(raw)
                    except ValueError:
                        errj = None
                    if isinstance(errj, dict):
                        em = str(errj.get('message') or '')
                        ec = errj.get('code')
                        el = em.lower()
                        if ec == 20015 or 'max_seq_len' in el or 'max_total_tokens' in el:
                            raise Exception(
                                '硅基流动：提示词与单次输出长度之和超过模型上下文上限（约 32K tokens）。'
                                '请缩小数据范围、缩短提示词或改用其它模型后重试。'
                            )
                err = Exception(f"HTTP {resp.status_code}: {detail[:200]}")
                if _mi + 1 < len(models_try):
                    logger.warning(
                        '硅基流动 model=%s 调用失败（%s），回退重试 model=%s — %s',
                        _mid,
                        resp.status_code,
                        models_try[_mi + 1],
                        detail[:120],
                    )
                    last_error = err
                    continue
                raise err

            try:
                data = resp.json()
            except ValueError:
                err = Exception(f"硅基流动返回非 JSON 响应: {(resp.text or '')[:200]}")
                if _mi + 1 < len(models_try):
                    logger.warning('硅基流动 model=%s 响应非 JSON，回退重试 model=%s', _mid, models_try[_mi + 1])
                    last_error = err
                    continue
                raise err

            if isinstance(data, dict) and 'choices' in data and data['choices']:
                choice = data['choices'][0]
                msg = (choice.get('message') or {})
                content = msg.get('content') or choice.get('text')
                if content:
                    if _mi > 0:
                        logger.info('硅基流动回退模型 %s 调用成功', _mid)
                    return content

            err = Exception(f"未知响应格式: {str(data)[:200]}")
            if _mi + 1 < len(models_try):
                logger.warning('硅基流动 model=%s 返回异常结构，回退重试 model=%s', _mid, models_try[_mi + 1])
                last_error = err
                continue
            raise err

        if last_error:
            raise last_error
        raise Exception('硅基流动调用失败')
    else:
        raise Exception(f"不支持的AI模型: {ai_config.selected_model}")


def generate_analysis_prompt(task, submission=None, file_content=None, SessionLocal=None, Submission=None, user_template=None, interface_desc=None):
    """根据任务信息生成分析提示词（优化版）
    
    Args:
        task: 任务对象
        submission: 提交数据列表
        file_content: 文件内容
        SessionLocal: 数据库会话工厂
        Submission: 提交模型类
        user_template: 用户自定义的提示词模板（可选），如果提供，将在模板中查找 {DATA_SECTION} 占位符并替换为数据部分
        interface_desc: 接口描述（可选），将作为背景信息参与生成
    """
    if not submission and SessionLocal and Submission:
        db = SessionLocal()
        try:
            submission = db.query(Submission).filter_by(task_id=task.id).all()
        finally:
            db.close()
    
    # 接口描述：优先使用传入的 interface_desc，否则使用任务描述
    desc_text = (interface_desc or '').strip() or (task.description or '无')
    
    # 生成数据部分
    data_section = f"""任务标题：{task.title}
接口描述（背景信息）：{desc_text}

提交数据信息：
"""
    
    # 生成数据详细内容
    if submission:
        total_count = len(submission)
        data_section += f"总提交数量：{total_count} 条\n\n"
        
        # 解析所有数据
        all_data = []
        field_types = {}  # 字段类型统计
        field_values = {}  # 字段值统计
        
        for sub in submission:
            try:
                data = _normalize_submission_payload(json.loads(sub.data))
                all_data.append(data)
                
                # 统计字段类型和值
                for key, value in data.items():
                    if key not in field_types:
                        field_types[key] = []
                        field_values[key] = []
                    
                    # 判断字段类型
                    if isinstance(value, (int, float)):
                        field_types[key].append('numeric')
                        field_values[key].append(value)
                    elif isinstance(value, bool):
                        field_types[key].append('boolean')
                        field_values[key].append(value)
                    else:
                        field_types[key].append('text')
                        field_values[key].append(str(value))
            except:
                pass
        
        # 添加字段统计信息
        if all_data and len(all_data) > 0:
            # 检查第一个数据项是否为字典类型
            first_item = all_data[0]
            if isinstance(first_item, dict):
                data_section += "数据字段统计：\n"
                for field in first_item.keys():
                    field_type_list = field_types.get(field, [])
                    if not field_type_list:
                        continue
                    
                    # 判断主要类型
                    is_numeric = field_type_list.count('numeric') > len(field_type_list) * 0.8
                    is_boolean = field_type_list.count('boolean') > len(field_type_list) * 0.8
                    
                    data_section += f"  - {field}: "
                    if is_numeric:
                        values = [v for v in field_values[field] if isinstance(v, (int, float))]
                        if values:
                            data_section += f"数值型，范围: {min(values)} - {max(values)}，平均值: {sum(values)/len(values):.2f}\n"
                        else:
                            data_section += "数值型\n"
                    elif is_boolean:
                        values = field_values[field]
                        true_count = sum(1 for v in values if v is True or str(v).lower() in ['true', '1', 'yes', '是'])
                        data_section += f"布尔型，是: {true_count}，否: {len(values)-true_count}\n"
                    else:
                        # 文本型，统计常见值
                        values = field_values[field]
                        value_counts = Counter(values)
                        top_values = value_counts.most_common(5)
                        if len(top_values) > 0:
                            data_section += f"文本型，常见值: {', '.join([f'{k}({v}次)' for k, v in top_values[:3]])}\n"
                        else:
                            data_section += "文本型\n"
                
                data_section += "\n"
        
        # 默认开启紧凑化，减少冗余 token（可通过环境变量关闭）
        compact_mode = COMPACT_MODE_ENABLED
        noise_set, keep_set = _get_compact_filter_sets()
        max_field_len = COMPACT_FIELD_MAX_LEN
        high_repeat_fields = _build_high_repeat_field_set(all_data, compact_mode, keep_set)
        if high_repeat_fields:
            show_n = max(1, REPEAT_SHOW_TOPN)
            preview = sorted(list(high_repeat_fields))[:show_n]
            data_section += "自动降噪字段（高重复低信息）：" + ", ".join(preview)
            if len(high_repeat_fields) > show_n:
                data_section += f" 等 {len(high_repeat_fields)} 项"
            data_section += "\n\n"
        # 智能采样：默认至少显示 100 条，超过 100 条后开始均匀抽样（仍显示约 100 条）
        SAMPLE_DISPLAY_MIN = 100  # 不超过此数量时全部显示；超过则抽样显示约 100 条
        if total_count <= 3:
            # 数据量少，全部显示
            data_section += "完整数据：\n"
            for i, data in enumerate(all_data, 1):
                data_section += f"\n#{i}\n"
                for key, value in data.items():
                    key_norm = (key or '').strip().lower()
                    if _should_skip_field(key, compact_mode, noise_set, keep_set) or key_norm in high_repeat_fields:
                        continue
                    if compact_mode:
                        value_str = _compact_field_value(value, max_field_len)
                    else:
                        if isinstance(value, (dict, list)):
                            try:
                                value_str = json.dumps(value, ensure_ascii=False)
                            except Exception:
                                value_str = str(value)
                        else:
                            value_str = str(value)
                    data_section += f"  - {key}: {value_str}\n"
        else:
            if total_count <= SAMPLE_DISPLAY_MIN:
                sample_indices = list(range(total_count))
                data_section += f"数据样例（共 {total_count} 条，全部显示）：\n"
            else:
                sample_size = SAMPLE_DISPLAY_MIN  # 超过 100 条时抽样显示 100 条
                head_n = min(35, total_count)
                sample_indices = list(range(0, head_n))
                if total_count > 80:
                    mid_start = total_count // 2 - 15
                    mid_end = total_count // 2 + 15
                    mid_start = max(head_n, mid_start)
                    mid_end = min(total_count - 35, mid_end)
                    if mid_end > mid_start:
                        sample_indices.extend(range(mid_start, mid_end))
                sample_indices.extend(range(max(0, total_count - 35), total_count))
                sample_indices = sorted(list(set(sample_indices)))[:sample_size]
                data_section += f"数据样例（共显示 {len(sample_indices)} 条，占总数的 {len(sample_indices)/total_count*100:.1f}%）：\n"
            for i in sample_indices:
                try:
                    data = all_data[i]
                    data_section += f"\n#{i + 1}\n"
                    for key, value in data.items():
                        key_norm = (key or '').strip().lower()
                        if _should_skip_field(key, compact_mode, noise_set, keep_set) or key_norm in high_repeat_fields:
                            continue
                        if compact_mode:
                            value_str = _compact_field_value(value, max_field_len)
                        else:
                            if isinstance(value, (dict, list)):
                                try:
                                    value_str = json.dumps(value, ensure_ascii=False)
                                except Exception:
                                    value_str = str(value)
                            else:
                                value_str = str(value)
                        data_section += f"  - {key}: {value_str}\n"
                except:
                    if i < len(submission):
                        fallback = submission[i].data
                        if compact_mode:
                            fallback = _compact_field_value(fallback, max_field_len * 2)
                        data_section += f"\n#{i + 1}\n{fallback}\n"
    else:
        data_section += "暂无提交数据\n"
    
    # 如果任务有HTML分析结果，添加到数据部分（与提交条数无关，过长则截断）
    if hasattr(task, 'html_analysis') and task.html_analysis:
        ha = (task.html_analysis or '').strip()
        if len(ha) > HTML_ANALYSIS_IN_PROMPT_MAX_CHARS:
            ha = ha[:HTML_ANALYSIS_IN_PROMPT_MAX_CHARS] + '\n…（HTML 分析内容已截断，完整内容在任务 HTML 分析结果中）'
        data_section += "\n\n【HTML文件分析结果】\n"
        data_section += ha
    
    # 根据是否有用户模板来决定如何组合最终的提示词
    if user_template and user_template.strip():
        # 如果提供了用户模板，将数据部分插入到模板中
        # 查找 {DATA_SECTION} 占位符，如果存在则替换，否则追加到模板末尾
        if '{DATA_SECTION}' in user_template:
            prompt = user_template.replace('{DATA_SECTION}', data_section)
        else:
            # 如果没有占位符，将数据部分追加到模板末尾
            prompt = user_template + "\n\n" + data_section
    else:
        # 使用默认模板
        prompt = f"""你是一个数据分析专家，请基于以下表单数据提供详细的分析报告：

{data_section}

请提供一个全面的数据分析报告，包括但不限于：
1. 数据概览：总提交量、关键数据分布、字段类型统计
2. 主要发现：数据中的趋势、模式、异常和相关性
3. 深入分析：基于数据的详细洞察，包括分布特征、集中趋势、离散程度等
4. 建议和结论：基于分析结果的实用建议和改进方向

请以中文撰写报告，使用Markdown格式，包括适当的标题、列表和表格来增强可读性。
"""
    
    return _collapse_extra_newlines(prompt)


def analyze_html_file(task_id, user_id, file_path, SessionLocal, Task, AIConfig, read_file_content_func, call_ai_model_func):
    """在后台分析HTML文件，将分析结果存储到数据库"""
    def analyze_in_background():
        print(f"[HTML分析] 后台分析任务开始，任务ID: {task_id}, 文件: {file_path}")
        db = SessionLocal()
        try:
            task = db.query(Task).filter_by(id=task_id, user_id=user_id).first()
            if not task:
                logger.warning(f"任务 {task_id} 不存在，跳过HTML分析")
                return
            
            # 读取HTML文件内容
            print(f"[HTML分析] 正在读取文件内容: {file_path}")
            html_content = read_file_content_func(file_path)
            if not html_content or len(html_content) < 100:
                print(f"[HTML分析] ⚠ HTML文件内容过短（{len(html_content) if html_content else 0} 字符），跳过分析")
                logger.warning(f"HTML文件内容过短，跳过分析")
                return
            print(f"[HTML分析] ✓ 文件内容读取成功，长度: {len(html_content)} 字符")
            
            # 获取用户的AI配置
            ai_config = db.query(AIConfig).filter_by(user_id=user_id).first()
            decrypt_ai_config_inplace(ai_config)
            if not ai_config:
                print(f"[HTML分析] ⚠ 用户 {user_id} 未配置AI，跳过HTML分析")
                logger.warning(f"用户 {user_id} 未配置AI，跳过HTML分析")
                return
            print(f"[HTML分析] ✓ AI配置获取成功，模型: {ai_config.selected_model}")
            
            # 生成分析提示词
            # 限制HTML内容长度，避免提示词过长
            html_preview = html_content[:5000] if len(html_content) > 5000 else html_content
            analysis_prompt = f"""请分析以下HTML文件的内容，提取关键信息，包括：
1. 页面的主要功能
2. 包含的主要内容
3. 可能的数据收集点或交互元素

HTML内容：
{html_preview}

请用简洁的中文总结，控制在200字以内。"""
            
            # 调用AI进行分析
            try:
                print(f"[HTML分析] → 正在调用AI模型进行分析...")
                analysis_result = call_ai_model_func(
                    analysis_prompt, ai_config, chat_server_model=get_chat_server_model_light()
                )
                # 保存分析结果到数据库
                task.html_analysis = analysis_result
                db.commit()
                print(f"[HTML分析] ✓ HTML文件分析完成，结果长度: {len(analysis_result) if analysis_result else 0} 字符")
                logger.info(f"任务 {task_id} 的HTML文件分析完成")
            except Exception as e:
                print(f"[HTML分析] ❌ AI分析失败: {str(e)}")
                logger.error(f"分析HTML文件失败: {str(e)}", exc_info=True)
                # 浏览器端无法看到后台线程日志：将可读错误写入任务，便于教师在任务详情/编辑页查看
                try:
                    from services.report_service import _to_user_friendly_ai_error

                    detail = (str(e) or "").strip()
                    friendly = _to_user_friendly_ai_error(detail)
                    note = f"【HTML分析失败】{friendly}"
                    if detail and detail not in friendly:
                        note = f"{note}\n\n（接口返回详情）{detail}"
                    if len(note) > 12000:
                        note = note[:12000] + "…"
                    task.html_analysis = note
                    db.commit()
                except Exception as save_err:
                    logger.warning("写入 HTML 分析失败说明到数据库时出错: %s", save_err)
        except Exception as e:
            print(f"[HTML分析] ❌ 后台分析任务失败: {str(e)}")
            logger.error(f"HTML分析后台任务失败: {str(e)}", exc_info=True)
        finally:
            db.close()
            print(f"[HTML分析] 后台分析任务结束\n")
    
    # 在后台线程中执行分析
    t = threading.Thread(target=analyze_in_background, daemon=True)
    t.start()


def generate_html_page_from_prompt(full_user_prompt, call_ai_model_func, ai_config):
    """
    根据用户需求描述生成完整单页 HTML。用于一键生成新任务。
    full_user_prompt: 用户输入的页面需求 + 已拼接的接口说明（含真实 API 地址）
    返回: 完整 HTML 字符串，若失败则抛出异常。
    """
    import re
    system = "你是一名前端开发助手。用户会给你一段需求描述，请你生成一个完整的、可直接在浏览器中运行的单一 HTML 文件。要求：只输出 HTML 代码，不要输出任何解释或 markdown 标记；不要用 ```html 包裹；文件需包含 <!DOCTYPE html>、<html>、<head>、<body>，样式可写在 <style> 或内联；确保表单提交、数据获取等逻辑与用户描述中的 API 地址一致。若需求中提到「获取已提交数据」或调用「API地址/all」：该接口返回的是 JSON 对象 { submissions: 数组, total_submissions: 数字 }，不是直接返回数组。前端必须用 data = await response.json() 后，用 data.submissions 取记录列表、用 data.total_submissions 取人数（或 data.submissions.length），切勿使用 data.length（根对象没有 length）。"
    prompt = f"""请根据以下需求生成一个完整的单页 HTML 文件（只输出 HTML，不要其他内容）：

{full_user_prompt}

请直接输出完整 HTML 代码，从 <!DOCTYPE html> 开始，到 </html> 结束。"""
    raw = call_ai_model_func(prompt, ai_config)
    if not raw or not raw.strip():
        raise Exception("AI 未返回有效内容")
    html = raw.strip()
    # 若被 markdown 代码块包裹则去掉
    m = re.search(r'```(?:html)?\s*([\s\S]*?)```', html, re.IGNORECASE)
    if m:
        html = m.group(1).strip()
    if '<!DOCTYPE' not in html.upper() and '<html' not in html:
        raise Exception("返回内容不是有效的 HTML")
    return html


def revise_html_with_ai(current_html, revision_instructions, call_ai_model_func, ai_config):
    """
    在现有 HTML 基础上按用户说明进行修改。用于编辑任务时的「AI 继续修改」。
    current_html: 当前完整 HTML 字符串
    revision_instructions: 用户输入的修改说明（如「把标题改成学生登记表，增加班级下拉框」）
    返回: 修改后的完整 HTML 字符串。
    """
    import re
    prompt = f"""下面是一段完整的单页 HTML 代码。请根据用户的「修改说明」在现有代码基础上进行修改，保持原有结构和 API 地址不变，只做用户要求的变化。只输出修改后的完整 HTML，不要用 markdown 代码块包裹，不要输出任何解释。

【当前 HTML】
{current_html}

【用户的修改说明】
{revision_instructions}

请直接输出从 <!DOCTYPE html> 到 </html> 的完整 HTML。"""
    raw = call_ai_model_func(prompt, ai_config)
    if not raw or not raw.strip():
        raise Exception("AI 未返回有效内容")
    html = raw.strip()
    m = re.search(r'```(?:html)?\s*([\s\S]*?)```', html, re.IGNORECASE)
    if m:
        html = m.group(1).strip()
    if '<!DOCTYPE' not in html.upper() and '<html' not in html:
        raise Exception("返回内容不是有效的 HTML")
    return html

