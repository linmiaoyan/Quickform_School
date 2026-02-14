"""AI服务 - 处理AI模型调用和分析相关功能"""
import json
import requests
import threading
import logging
from datetime import datetime
from collections import Counter
from flask import current_app

logger = logging.getLogger(__name__)


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
                {"role": "system", "content": "你面向的用户一般是教师和学生"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 4000
        }
        
        try:
            response = requests.post(url, headers=headers, json=data, timeout=60)
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except Exception as e:
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
                    {"role": "system", "content": "你面向的用户一般是教师和学生"},
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
        url = 'https://api.siliconflow.cn/v1/chat/completions'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
        payload = {
            'model': 'deepseek-ai/DeepSeek-V2.5',
            'messages': [
                {"role": "system", "content": "你面向的用户一般是教师和学生"},
                {"role": "user", "content": prompt}
            ]
        }
        try:
            resp = _requests.post(url, headers=headers, json=payload, timeout=(5, 120))
            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            # OpenAI兼容结构
            if isinstance(data, dict) and 'choices' in data and data['choices']:
                choice = data['choices'][0]
                # message.content 或 text
                msg = (choice.get('message') or {})
                content = msg.get('content') or choice.get('text')
                if content:
                    return content
            raise Exception(f"未知响应格式: {str(data)[:200]}")
        except _requests.Timeout as e:
            logger.error(f"硅基流动超时: {e}")
            raise Exception(f"硅基流动超时: {e}")
        except Exception as e:
            logger.error(f"硅基流动调用失败: {str(e)}")
            raise Exception(f"硅基流动调用失败: {str(e)}")
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
                data = json.loads(sub.data)
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
        
        # 智能采样：根据数据量决定显示多少条
        sample_size = min(20, total_count)  # 最多显示20条
        if total_count > 3:
            # 如果有大量数据，均匀采样（首、中、尾）
            if total_count <= sample_size:
                sample_indices = list(range(total_count))
            else:
                # 均匀采样：取前几条、中间几条、后几条
                sample_indices = list(range(0, min(5, total_count)))  # 前5条
                if total_count > 10:
                    mid_start = total_count // 2 - 3
                    mid_end = total_count // 2 + 3
                    sample_indices.extend(range(mid_start, mid_end))
                sample_indices.extend(range(max(0, total_count - 5), total_count))  # 后5条
                sample_indices = sorted(list(set(sample_indices)))[:sample_size]
            
            data_section += f"数据样例（共显示 {len(sample_indices)} 条，占总数的 {len(sample_indices)/total_count*100:.1f}%）：\n"
            for idx, i in enumerate(sample_indices, 1):
                try:
                    data = all_data[i]
                    data_section += f"\n样例 #{idx} (第 {i+1} 条记录):\n"
                    for key, value in data.items():
                        # 限制单个值长度，避免过长
                        value_str = str(value)
                        if len(value_str) > 100:
                            value_str = value_str[:100] + "...[截断]"
                        data_section += f"  - {key}: {value_str}\n"
                except:
                    if i < len(submission):
                        data_section += f"\n样例 #{idx}: {submission[i].data[:100]}...\n"
        else:
            # 数据量少，全部显示
            data_section += "完整数据：\n"
            for i, data in enumerate(all_data, 1):
                data_section += f"\n提交 #{i}:\n"
                for key, value in data.items():
                    value_str = str(value)
                    if len(value_str) > 100:
                        value_str = value_str[:100] + "...[截断]"
                    data_section += f"  - {key}: {value_str}\n"
    else:
        data_section += "暂无提交数据\n"
    
    # 如果任务有HTML分析结果，添加到数据部分
    if hasattr(task, 'html_analysis') and task.html_analysis:
        data_section += "\n\n【HTML文件分析结果】\n"
        data_section += task.html_analysis
    
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
    
    return prompt


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
                analysis_result = call_ai_model_func(analysis_prompt, ai_config)
                # 保存分析结果到数据库
                task.html_analysis = analysis_result
                db.commit()
                print(f"[HTML分析] ✓ HTML文件分析完成，结果长度: {len(analysis_result) if analysis_result else 0} 字符")
                logger.info(f"任务 {task_id} 的HTML文件分析完成")
            except Exception as e:
                print(f"[HTML分析] ❌ AI分析失败: {str(e)}")
                logger.error(f"分析HTML文件失败: {str(e)}", exc_info=True)
        except Exception as e:
            print(f"[HTML分析] ❌ 后台分析任务失败: {str(e)}")
            logger.error(f"HTML分析后台任务失败: {str(e)}", exc_info=True)
        finally:
            db.close()
            print(f"[HTML分析] 后台分析任务结束\n")
    
    # 在后台线程中执行分析
    t = threading.Thread(target=analyze_in_background, daemon=True)
    t.start()

