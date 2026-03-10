"""工具函数和AI相关功能"""
import os
import json
import requests
import threading
import logging
import traceback
import uuid
from datetime import datetime
from functools import wraps

logger = logging.getLogger(__name__)

# 全局变量（仅HTML格式）
ALLOWED_EXTENSIONS = {'html', 'htm'}
analysis_progress = {}
analysis_results = {}
completed_reports = set()
progress_lock = threading.Lock()


def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_file(file, upload_folder):
    """保存上传的文件"""
    try:
        if file and allowed_file(file.filename):
            unique_filename = str(uuid.uuid4()) + '_' + file.filename
            filepath = os.path.join(upload_folder, unique_filename)
            file.save(filepath)
            return unique_filename, filepath
    except Exception as e:
        logger.error(f"保存文件失败: {str(e)}")
    return None, None


def read_file_content(file_path):
    """读取文件内容"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
                return f"二进制文件 (大小: {len(content)} 字节)"
        except Exception as e:
            logger.error(f"读取文件内容失败: {str(e)}")
            return f"无法读取文件内容: {str(e)}"
    except Exception as e:
        logger.error(f"读取文件内容失败: {str(e)}")
        return f"无法读取文件内容: {str(e)}"


def generate_analysis_prompt(task, submission=None, file_content=None, SessionLocal=None):
    """根据任务信息生成分析提示词"""
    if not submission and SessionLocal:
        db = SessionLocal()
        try:
            import models
            submission = db.query(models.Submission).filter_by(task_id=task.id).all()
        finally:
            db.close()
    
    prompt = f"""你是一个数据分析专家，请基于以下表单数据提供详细的分析报告：

任务标题：{task.title}
任务描述：{task.description or '无'}

提交数据摘要：
"""
    
    if submission:
        prompt += f"共有 {len(submission)} 条提交记录\n"
        for i, sub in enumerate(submission[:3]):
            try:
                data = json.loads(sub.data)
                prompt += f"\n提交 #{i+1}:\n"
                for key, value in data.items():
                    prompt += f"  - {key}: {value}\n"
            except:
                prompt += f"\n提交 #{i+1}: {sub.data[:100]}...\n"
    else:
        prompt += "暂无提交数据\n"
    
    if file_content:
        prompt += f"\n附件内容摘要：\n{file_content[:500]}...\n" if len(file_content) > 500 else f"\n附件内容：\n{file_content}\n"
    
    prompt += """

请提供一个全面的数据分析报告，包括但不限于：
1. 数据概览：总提交量、关键数据分布等
2. 主要发现：数据中的趋势、模式和异常
3. 深入分析：基于数据的详细洞察
4. 建议和结论：基于分析结果的实用建议

请以中文撰写报告，使用Markdown格式，包括适当的标题、列表和表格来增强可读性。
"""
    
    return prompt


def _parse_deepseek_error(response):
    """从 DeepSeek API 错误响应中提取可读信息"""
    try:
        body = (response.text or "").strip()
        if not body:
            return response.reason or "无响应内容"
        data = json.loads(body)
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return err["message"]
        if data.get("message"):
            return data["message"]
        return body[:400]
    except Exception:
        return (response.text or response.reason or "未知错误")[:400]


def call_ai_model(prompt, ai_config):
    """调用AI模型生成分析报告"""
    if ai_config.selected_model == 'deepseek':
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ai_config.deepseek_api_key}"
        }
        data = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你是一个专业的数据分析助手。请基于用户提供的数据，生成一份详细、专业、有洞察力的分析报告。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 4000
        }
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=60)
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
                {"role": "system", "content": "你是一个专业的数据分析助手。请基于用户提供的数据，生成一份详细、专业、有洞察力的分析报告。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 4000
        }
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=120)
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
                    {"role": "system", "content": "你是一个专业的数据分析助手。请基于用户提供的数据，生成一份详细、专业、有洞察力的分析报告。"},
                    {"role": "user", "content": prompt}
                ]
            },
            "parameters": {
                "temperature": 0.7,
                "max_tokens": 4000
            }
        }
        
        try:
            logger.info(f"调用阿里云百炼API，模型: qwen-plus")
            response = requests.post(url, headers=headers, json=data, timeout=120)
            
            if response.status_code != 200:
                raise Exception(f"阿里云百炼API调用失败，状态码: {response.status_code}，响应: {response.text[:200]}")
            
            if not response.text:
                raise Exception("阿里云百炼API返回空响应")
            
            result = response.json()
            
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
    
    else:
        raise Exception(f"不支持的AI模型: {ai_config.selected_model}")


def save_analysis_report(task_id, report_content, SessionLocal, upload_folder):
    """保存分析报告到文件系统和数据库"""
    import models
    
    db = SessionLocal()
    try:
        task = db.query(models.Task).filter_by(id=task_id).first()
        if task:
            if not report_content or not report_content.strip():
                report_content = "<div class='alert alert-info' role='alert'><h4>报告内容为空</h4><p>本次分析未能生成有效内容。可能是由于以下原因：</p><ul><li>提交的数据量不足</li><li>数据质量问题</li><li>AI模型处理异常</li></ul><p>请尝试提交更多数据或修改提示词后重新分析。</p></div>"
            
            html_report = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>分析报告 - {task.title}</title>
    <!-- Bootstrap CSS已通过base.html引入，此处不再重复引入 -->
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 40px 20px; background-color: #f8f9fa; }}
        .container {{ background-color: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1); }}
        .markdown-body {{ font-size: 16px; }}
        .markdown-body h1, .markdown-body h2, .markdown-body h3 {{ color: #2c3e50; }}
        .markdown-body pre {{ background-color: #f6f8fa; border-radius: 6px; }}
        .footer {{ text-align: center; margin-top: 40px; padding: 20px; color: #6c757d; font-size: 0.9rem; }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="mb-4">数据分析报告</h1>
        <p><strong>任务标题：</strong>{task.title}</p>
        <p><strong>创建时间：</strong>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <div class="markdown-body">{report_content}</div>
        <div class="footer"><p>由 QuickForm 智能分析功能生成</p></div>
    </div>
</body>
</html>"""
            
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

