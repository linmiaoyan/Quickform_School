"""报告生成服务"""
import os
import io
import re
import zipfile
import urllib.parse
import threading
import logging
from datetime import datetime
from functools import wraps
from PIL import Image, ImageDraw, ImageFont
from core.secret_store import decrypt_ai_config_inplace

logger = logging.getLogger(__name__)

# 单张图片最大高度，超过则分多张输出，避免长图右侧被截断
MAX_IMAGE_HEIGHT = 4000

# 用于存储分析任务进度的字典
analysis_progress = {}
analysis_results = {}
completed_reports = set()
progress_lock = threading.Lock()

# 分批分析默认固定参数（不依赖 .env）
MULTI_BATCH_ENABLED = True
MULTI_BATCH_TRIGGER_CHARS = 28000
MULTI_BATCH_CHUNK_SIZE = 18000
MULTI_BATCH_OVERLAP = 300
MULTI_BATCH_MAX_BATCHES = 8


def _to_user_friendly_ai_error(err_msg):
    """将模型/网关错误转换为可读提示，避免直接暴露 Internal Server Error。"""
    msg = (err_msg or '').strip()
    low = msg.lower()
    # 超时优先：装饰器与部分网关会返回含「超时」的中文，或英文 timeout / timed out。
    # 勿使用宽泛子串「too long」——否则会与「request took too long」等误判为「数据量过大」。
    if any(k in low for k in ['504', 'gateway timeout', 'timed out', 'timeout']) or '超时' in msg:
        return "分析请求超时（单次调用最长约 4 分钟），请稍后重试；也可缩小数据范围提升成功率。"
    if any(k in low for k in ['413', 'payload too large', 'request entity too large']):
        return "请求数据过大（413）。请缩小数据范围后重试。"
    # 仅匹配「上下文/输入过长」类表述。勿单独匹配「token」——否则会与 invalid token、API key 等鉴权错误混淆。
    context_limit_markers = (
        'context length',
        'maximum context',
        'max context',
        'context window',
        'exceeded the context',
        'exceed context',
        'token limit',
        'maximum token',
        'too many tokens',
        'tokens exceed',
        'total tokens',
        'input length',
        'input is too long',
        'prompt is too long',
        'message is too long',
        'max_tokens',
        'length limited',
        'reduce your prompt',
        'shorten the prompt',
    )
    if any(m in low for m in context_limit_markers):
        return "本次分析数据量过大，超过模型可处理上限。请缩小日期范围、减少样本后重试。"
    if any(k in low for k in ['500', 'internal server error']):
        return "服务暂时异常（500），请稍后重试；若数据量较大建议先缩小范围。"
    if any(k in low for k in ['402', 'insufficient balance', '余额不足', 'insufficient_quota', 'payment required']):
        return "API 账户余额或额度不足（常见为 HTTP 402）。请登录相应服务商控制台充值，或更换可用的 API Key 后再试。"
    if any(k in low for k in ['401', 'unauthorized', 'invalid api key', 'invalid_api_key', 'incorrect api key']):
        return "API 密钥无效或未授权（401）。请在个人中心核对所选模型与 API Key 是否一致、是否复制完整。"
    if '429' in low or 'rate limit' in low or 'too many requests' in low or '限流' in msg:
        return "请求过于频繁被限流（429）。请稍后再试，或升级服务商套餐。"
    return f"模型调用失败：{msg}" if msg else "模型调用失败，请稍后重试。"


def timeout(seconds, error_message="函数执行超时"):
    """超时装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = [None]
            exception = [None]
            
            def target():
                try:
                    result[0] = func(*args, **kwargs)
                except Exception as e:
                    exception[0] = e
            
            thread = threading.Thread(target=target)
            thread.daemon = True
            thread.start()
            thread.join(seconds)
            
            if thread.is_alive():
                raise TimeoutError(error_message)
            elif exception[0]:
                raise exception[0]
            else:
                return result[0]
        
        return wrapper
    return decorator


def save_analysis_report(task_id, report_content, SessionLocal, Task, upload_folder):
    """保存分析报告到文件系统和数据库"""
    db = SessionLocal()
    try:
        task = db.query(Task).filter_by(id=task_id).first()
        if task:
            safe_title = (getattr(task, 'title', None) or f'任务{task_id}').strip() or f'任务{task_id}'
            if not report_content or not report_content.strip():
                report_content = "<div class='alert alert-info' role='alert'><h4>报告内容为空</h4><p>本次分析未能生成有效内容。可能是由于以下原因：</p><ul><li>提交的数据量不足</li><li>数据质量问题</li><li>AI模型处理异常</li></ul><p>请尝试提交更多数据或修改提示词后重新分析。</p></div>"
                body_html = report_content
            else:
                body_html = markdown_to_html(report_content)
            html_report = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>分析报告 - {safe_title}</title>
    <!-- Bootstrap CSS已通过base.html引入，此处不再重复引入 -->
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 40px 20px;
            background-color: #f8f9fa;
        }}
        .container {{
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }}
        .markdown-body {{
            font-size: 16px;
        }}
        .markdown-body h1, .markdown-body h2, .markdown-body h3 {{
            color: #2c3e50;
        }}
        .markdown-body pre {{
            background-color: #f6f8fa;
            border-radius: 6px;
        }}
        .footer {{
            text-align: center;
            margin-top: 40px;
            padding: 20px;
            color: #6c757d;
            font-size: 0.9rem;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="mb-4">数据分析报告</h1>
        <p><strong>任务标题：</strong>{task.title}</p>
        <p><strong>创建时间：</strong>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        
        <div class="markdown-body">
            {body_html}
        </div>
        
        <div class="footer">
            <p>由 QuickForm 智能分析功能生成</p>
        </div>
    </div>
</body>
</html>
            """
            
            report_dir = os.path.join(upload_folder, 'reports')
            if not os.path.exists(report_dir):
                os.makedirs(report_dir)
            
            report_filename = f"report_{task_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            report_path = os.path.join(report_dir, report_filename)
            
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(html_report)
            
            task.analysis_report = report_content
            task.report_file_path = report_path
            task.report_generated_at = datetime.now()
            db.commit()
            
            with progress_lock:
                completed_reports.add(task_id)
            
            logger.info(f"任务 {task_id} 的分析报告已保存")
    except Exception as e:
        logger.error(f"保存分析报告失败: {str(e)}")
    finally:
        db.close()


def markdown_to_html(text):
    """将 Markdown 文本转为 HTML，用于报告展示与导出。若转换失败则返回原文。"""
    if not text or not text.strip():
        return text
    try:
        import markdown
        return markdown.markdown(text, extensions=['extra', 'nl2br'])
    except Exception:
        return text


def build_report_html(task, report_content, for_pdf=False):
    """构建报告完整 HTML 字符串，用于 HTML 下载或 PDF 生成；报告正文中的 Markdown 会被渲染为 HTML。
    for_pdf=True 时使用中文字体（如 STSong-Light），以便 xhtml2pdf/weasyprint 正确显示中文。"""
    if not report_content or not report_content.strip():
        body_html = "<div class='alert alert-info' role='alert'><h4>报告内容为空</h4><p>暂无有效报告内容。</p></div>"
    else:
        body_html = markdown_to_html(report_content)
    safe_title = (getattr(task, 'title', None) or f'任务{getattr(task, "id", "") or ""}').strip() or '任务'
    created_str = task.created_at.strftime('%Y-%m-%d %H:%M:%S') if getattr(task, 'created_at', None) else '未知'
    # PDF 导出需指定支持中文的字体：xhtml2pdf 内置 STSong-Light
    if for_pdf:
        font_css = (
            "body { font-family: STSong-Light, 'SimSun', 'Microsoft YaHei', 'PingFang SC', serif; "
            "line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 40px 20px; background-color: #f8f9fa; }"
        )
    else:
        font_css = (
            "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
            "line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 40px 20px; background-color: #f8f9fa; }"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>分析报告 - {safe_title}</title>
    <style>
        {font_css}
        .container {{ background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
        .markdown-body {{ font-size: 16px; }}
        .markdown-body h1, .markdown-body h2, .markdown-body h3 {{ color: #2c3e50; }}
        .markdown-body pre {{ background-color: #f6f8fa; border-radius: 6px; padding: 12px; }}
        .markdown-body ul, .markdown-body ol {{ margin: 0.5em 0; padding-left: 1.5em; }}
        .markdown-body table {{ border-collapse: collapse; width: 100%; }}
        .markdown-body th, .markdown-body td {{ border: 1px solid #ddd; padding: 8px; }}
        .footer {{ text-align: center; margin-top: 40px; padding: 20px; color: #6c757d; font-size: 0.9rem; }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="mb-4">数据分析报告</h1>
        <p><strong>任务标题：</strong>{safe_title}</p>
        <p><strong>创建时间：</strong>{created_str}</p>
        <div class="markdown-body">{body_html}</div>
        <div class="footer"><p>由 QuickForm 智能分析功能生成</p></div>
    </div>
</body>
</html>"""


def generate_report_image(task, report_content):
    """生成报告图片（PNG格式）"""
    img_width = 1200
    padding = 50
    max_width = img_width - 2 * padding
    task_title = (getattr(task, 'title', None) or '').strip() or f"task_{getattr(task, 'id', 'unknown')}"
    
    # 尝试加载字体（如果系统有中文字体）
    try:
        title_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 32)  # 微软雅黑
        heading_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 24)
        normal_font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
    except:
        try:
            title_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 32)  # 黑体
            heading_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 24)
            normal_font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 18)
        except:
            title_font = ImageFont.load_default()
            heading_font = ImageFont.load_default()
            normal_font = ImageFont.load_default()
    
    # 创建用于测量的画布
    dummy_img = Image.new('RGB', (img_width, 1), color='white')
    dummy_draw = ImageDraw.Draw(dummy_img)
    
    def measure(text, font):
        try:
            bbox = dummy_draw.textbbox((0, 0), text, font=font)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            return dummy_draw.textsize(text, font=font)
    
    render_items = []
    current_y = padding
    
    def add_text_line(text, font, fill='#000000', align='left', extra_spacing=10):
        nonlocal current_y
        if not text:
            current_y += extra_spacing
            return
        width, height = measure(text, font)
        render_items.append({
            'text': text,
            'font': font,
            'fill': fill,
            'align': align,
            'y': current_y
        })
        current_y += height + extra_spacing
    
    def wrap_lines(text, font):
        words = text.split()
        if not words:
            return []
        lines = []
        current_line = ""
        for word in words:
            candidate = (current_line + " " + word).strip()
            width, _ = measure(candidate, font)
            if width <= max_width:
                current_line = candidate
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        return lines
    
    # 标题
    add_text_line("数据分析报告", title_font, fill='#1a73e8', align='center', extra_spacing=30)
    
    # 任务信息
    info_lines = [
        f"任务标题：{task_title}",
        f"创建时间：{task.created_at.strftime('%Y-%m-%d %H:%M:%S') if task.created_at else '未知'}"
    ]
    for line in info_lines:
        add_text_line(line, normal_font, extra_spacing=8)
    current_y += 10
    
    # 处理报告内容 - 使用markdown渲染
    try:
        import markdown
        from bs4 import BeautifulSoup
        html_content = markdown.markdown(report_content, extensions=['extra', 'nl2br'])
        soup = BeautifulSoup(html_content, 'html.parser')
        text_content = soup.get_text('\n')
    except:
        text_content = report_content
        text_content = re.sub(r'\*\*(.+?)\*\*', r'\1', text_content)
        text_content = re.sub(r'\*(.+?)\*', r'\1', text_content)
        text_content = re.sub(r'`(.+?)`', r'\1', text_content)
    
    paragraphs = text_content.split('\n\n')
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            current_y += 15
            continue
        
        if para.startswith('##'):
            heading_text = para.replace('##', '').strip()
            for line in wrap_lines(heading_text, heading_font):
                add_text_line(line, heading_font, fill='#333333', extra_spacing=12)
            current_y += 8
        elif para.startswith('#'):
            heading_text = para.replace('#', '').strip()
            add_text_line(heading_text, heading_font, fill='#333333', extra_spacing=15)
        else:
            lines = para.split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith('- ') or line.startswith('* '):
                    line = '• ' + line[2:].strip()
                elif re.match(r'^\d+\.\s+', line):
                    line = '• ' + re.sub(r'^\d+\.\s+', '', line)
                
                wrapped = wrap_lines(line, normal_font)
                if not wrapped:
                    wrapped = [line]
                for text_line in wrapped:
                    add_text_line(text_line, normal_font, extra_spacing=6)
            current_y += 4
    
    img_height = max(current_y + padding, padding * 2)
    img = Image.new('RGB', (img_width, img_height), color='white')
    draw = ImageDraw.Draw(img)
    
    for item in render_items:
        text = item['text']
        font = item['font']
        fill = item['fill']
        y = item['y']
        align = item['align']
        width, _ = measure(text, font)
        if align == 'center':
            x = (img_width - width) // 2
        else:
            x = padding
        draw.text((x, y), text, font=font, fill=fill)
    
    safe_title = re.sub(r'[^a-zA-Z0-9_]', '_', task_title)
    buffers = []
    filenames = []
    
    if img_height <= MAX_IMAGE_HEIGHT:
        buffer = io.BytesIO()
        img.save(buffer, format='PNG', optimize=True)
        buffer.seek(0)
        buffers.append(buffer)
        filenames.append(f"{safe_title}_report.png")
    else:
        # 分多张输出，每张高度不超过 MAX_IMAGE_HEIGHT
        y_start = 0
        page = 1
        while y_start < img_height:
            y_end = min(y_start + MAX_IMAGE_HEIGHT, img_height)
            chunk = img.crop((0, y_start, img_width, y_end))
            buf = io.BytesIO()
            chunk.save(buf, format='PNG', optimize=True)
            buf.seek(0)
            buffers.append(buf)
            filenames.append(f"{safe_title}_report_{page}.png")
            y_start = y_end
            page += 1
    
    return buffers, filenames


def _user_can_access_task_for_report(db, task, user_id, User, OrganizationMember, TaskShare):
    """
    与 smart_analyze / download_report 一致：所有者、管理员、被共享者、组织成员可生成/保存报告。
    后台线程中不能使用 current_user，故单独校验。
    """
    user = db.get(User, user_id)
    if not user:
        return False
    if user.is_admin() or task.user_id == user_id:
        return True
    if db.query(TaskShare).filter_by(task_id=task.id, user_id=user_id).first():
        return True
    if task.organization_id:
        return db.query(OrganizationMember).filter_by(
            organization_id=task.organization_id,
            user_id=user_id
        ).first() is not None
    return False


def _chunk_text_by_size(text, chunk_size, overlap=0):
    """按字符数切分文本，尽量在换行边界截断。"""
    if not text:
        return []
    chunks = []
    n = len(text)
    start = 0
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            cut = text.rfind('\n', start, end)
            if cut > start + int(chunk_size * 0.5):
                end = cut
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = end - max(0, overlap)
        if start < 0:
            start = 0
    return chunks


def _rebalance_chunks(chunks, max_batches):
    """当分片过多时合并相邻分片，控制批次数。"""
    if not chunks or len(chunks) <= max_batches:
        return chunks
    merged = []
    group_size = (len(chunks) + max_batches - 1) // max_batches
    for i in range(0, len(chunks), group_size):
        merged.append("\n\n".join(chunks[i:i + group_size]))
    return merged


def perform_analysis_with_custom_prompt(task_id, user_id, ai_config_id, custom_prompt, 
                                         SessionLocal, Task, Submission, AIConfig,
                                         read_file_content_func, call_ai_model_func, 
                                         save_analysis_report_func,
                                         User, OrganizationMember, TaskShare):
    """使用自定义提示词执行分析任务"""
    import traceback
    import logging
    
    db = SessionLocal()
    try:
        task = db.query(Task).filter_by(id=task_id).first()
        if not task:
            with progress_lock:
                analysis_progress[task_id] = {
                    'status': 'error',
                    'message': '任务不存在'
                }
            return
        if not _user_can_access_task_for_report(db, task, user_id, User, OrganizationMember, TaskShare):
            with progress_lock:
                analysis_progress[task_id] = {
                    'status': 'error',
                    'message': '无权访问此任务'
                }
            return
        
        submission = db.query(Submission).filter_by(task_id=task_id).all()
        
        file_content = None
        if task.file_path and os.path.exists(task.file_path):
            file_content = read_file_content_func(task.file_path)
        
        ai_config = db.query(AIConfig).filter_by(id=ai_config_id).first()
        decrypt_ai_config_inplace(ai_config)
        if not ai_config:
            with progress_lock:
                analysis_progress[task_id] = {
                    'status': 'error',
                    'message': 'AI配置不存在'
                }
            return
        
        if ai_config.selected_model == 'deepseek' and not ai_config.deepseek_api_key:
            with progress_lock:
                analysis_progress[task_id] = {
                    'status': 'error',
                    'message': 'DeepSeek API密钥未配置'
                }
            logging.error(f"任务 {task_id}：DeepSeek API密钥未配置")
            return
        elif ai_config.selected_model == 'doubao' and not ai_config.doubao_api_key:
            with progress_lock:
                analysis_progress[task_id] = {
                    'status': 'error',
                    'message': '豆包API密钥未配置完整'
                }
            logging.error(f"任务 {task_id}：豆包API密钥未配置完整")
            return
        
        logging.info(f"任务 {task_id}：使用模型 {ai_config.selected_model}")
        
        with progress_lock:
            analysis_progress[task_id] = {
                'status': 'in_progress',
                'progress': 0,
                'message': '正在生成提示词...'
            }
        
        prompt = custom_prompt or ""
        
        with progress_lock:
            analysis_progress[task_id] = {
                'status': 'in_progress',
                'progress': 1,
                'message': '大模型分析中，单次调用最长约 4 分钟，请稍候...'
            }
        logging.info(f"任务 {task_id}：调用AI模型进行分析")
        
        # 单次模型调用超时（与 ai_service 中 requests 超时对齐）；多批分析时每批独立计时
        timeout_seconds = 240
        
        @timeout(seconds=timeout_seconds, error_message=f"调用{ai_config.selected_model}模型超时（{timeout_seconds}秒）")
        def call_ai_with_timeout(prompt, config):
            logging.info(f"开始调用 {config.selected_model} API，提示词长度: {len(prompt)} 字符，超时设置: {timeout_seconds}秒")
            return call_ai_model_func(prompt, config)
        
        try:
            chunk_enabled = MULTI_BATCH_ENABLED
            chunk_trigger_chars = MULTI_BATCH_TRIGGER_CHARS
            chunk_size = MULTI_BATCH_CHUNK_SIZE
            chunk_overlap = MULTI_BATCH_OVERLAP
            max_batches = MULTI_BATCH_MAX_BATCHES

            should_batch = chunk_enabled and len(prompt) >= chunk_trigger_chars
            if should_batch:
                raw_chunks = _chunk_text_by_size(prompt, chunk_size=max(2000, chunk_size), overlap=max(0, chunk_overlap))
                chunks = _rebalance_chunks(raw_chunks, max_batches=max(2, max_batches))
                total_batches = len(chunks)
                logging.info(f"任务 {task_id}：启用多批分析，原始分片 {len(raw_chunks)}，实际批次 {total_batches}")

                batch_summaries = []
                for idx, part in enumerate(chunks, 1):
                    with progress_lock:
                        analysis_progress[task_id] = {
                            'status': 'in_progress',
                            'progress': min(95, int((idx - 1) * 100 / max(total_batches + 1, 1))),
                            'message': f'分批分析中（第 {idx}/{total_batches} 批）...',
                            'batch_index': idx,
                            'batch_total': total_batches
                        }
                    batch_prompt = (
                        "你将收到一段较长数据分析任务的分片内容，请只输出本分片的关键结论（Markdown）。"
                        "要求：\n"
                        "1) 输出本分片中的数据特征、异常、趋势、建议；\n"
                        "2) 不要编造本分片中不存在的信息；\n"
                        "3) 简洁但保留关键数字与证据。\n\n"
                        f"【分片 {idx}/{total_batches} 开始】\n{part}\n【分片结束】"
                    )
                    batch_result = call_ai_with_timeout(batch_prompt, ai_config)
                    batch_summaries.append(f"## 分片{idx}结论\n{(batch_result or '').strip()}")

                with progress_lock:
                    analysis_progress[task_id] = {
                        'status': 'in_progress',
                        'progress': 96,
                        'message': '正在汇总分批结果...'
                    }
                merge_prompt = (
                    "请将以下多个分片结论整合为一份完整中文分析报告（Markdown）。\n"
                    "要求：\n"
                    "1) 合并重复观点，保留关键数据依据；\n"
                    "2) 给出整体结论和可执行建议；\n"
                    "3) 若分片之间有冲突，请明确说明。\n\n"
                    + "\n\n".join(batch_summaries)
                )
                analysis_report = call_ai_with_timeout(merge_prompt, ai_config)
            else:
                analysis_report = call_ai_with_timeout(prompt, ai_config)

            logging.info(f"成功获取 {ai_config.selected_model} API 响应，报告长度: {len(analysis_report)} 字符")
        except TimeoutError as timeout_error:
            error_msg = str(timeout_error)
            logging.error(f"任务 {task_id}：{error_msg}")
            with progress_lock:
                analysis_progress[task_id] = {
                    'status': 'error',
                    'message': _to_user_friendly_ai_error(error_msg)
                }
            return
        except Exception as api_error:
            logging.error(f"任务 {task_id}：AI模型调用失败: {str(api_error)}")
            logging.error(f"详细错误堆栈: {traceback.format_exc()}")
            with progress_lock:
                analysis_progress[task_id] = {
                    'status': 'error',
                    'message': _to_user_friendly_ai_error(str(api_error))
                }
            return
        
        if analysis_report.startswith("错误：") or \
           (analysis_report.startswith("DeepSeek API调用") and "失败" in analysis_report) or \
           (analysis_report.startswith("豆包API调用") and "失败" in analysis_report):
            logging.error(f"任务 {task_id}：AI模型返回错误: {analysis_report}")
            raise Exception(analysis_report)
        
        with progress_lock:
            # 先保存到内存，确保状态查询能立即获取
            analysis_results[task_id] = analysis_report
            analysis_progress[task_id] = {
                'status': 'completed',
                'progress': 100,
                'message': '分析完成，请查看报告',
                'report': analysis_report  # 直接包含在progress中，确保前端能获取
            }
            logger.info(f"任务 {task_id} 报告已保存到内存，长度: {len(analysis_report)} 字符")
        
        # 保存到数据库（在锁外执行，避免阻塞状态查询）
        try:
            # 获取upload_folder路径
            quickform_dir = os.path.dirname(os.path.abspath(__file__))
            upload_folder = os.path.join(quickform_dir, 'uploads')
            save_analysis_report_func(task_id, analysis_report, SessionLocal, Task, upload_folder)
            logger.info(f"任务 {task_id} 报告已保存到数据库")
        except Exception as e:
            logger.error(f"保存报告到数据库失败 - Task ID: {task_id}, 错误: {str(e)}")
            # 即使数据库保存失败，内存中已有报告，不影响用户查看
            
    except Exception as e:
        with progress_lock:
            analysis_progress[task_id] = {
                'status': 'error',
                'message': _to_user_friendly_ai_error(str(e)),
            }
    finally:
        db.close()

