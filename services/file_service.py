"""文件处理服务"""
import os
import uuid
import logging
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 允许的文件扩展名（仅HTML格式）
ALLOWED_EXTENSIONS = {'html', 'htm'}

# 认证文件允许的扩展名
CERTIFICATION_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}


def allowed_file(filename, allowed_extensions=None):
    """检查文件扩展名是否允许
    
    Args:
        filename: 文件名
        allowed_extensions: 允许的扩展名集合，如果为None则使用默认的ALLOWED_EXTENSIONS
    
    Returns:
        bool: 是否允许
    """
    if allowed_extensions is None:
        allowed_extensions = ALLOWED_EXTENSIONS
    
    if not filename or '.' not in filename:
        logger.warning(f"allowed_file: 文件名无效或没有扩展名 - {filename}")
        return False
    
    # 获取扩展名并转换为小写，去除可能的空白字符
    ext = filename.rsplit('.', 1)[1].lower().strip()
    # 去除可能的BOM或其他隐藏字符
    ext = ext.replace('\ufeff', '').replace('\u200b', '').strip()
    
    result = ext in allowed_extensions
    if not result:
        filename_bytes = filename.encode('utf-8', errors='replace') if filename else b''
        logger.warning(f"文件格式不允许 - 文件名: {filename}, 扩展名: '{ext}', 允许的格式: {allowed_extensions}")
    return result


def decode_html_bytes(data: bytes) -> str:
    """将 HTML 文件字节解码为 Unicode 文本，兼容 UTF-8/UTF-16（含无 BOM 的 UTF-16LE）等常见编码。"""
    if not data:
        return ''
    if data.startswith(b'\xff\xfe'):
        return data.decode('utf-16-le')
    if data.startswith(b'\xfe\xff'):
        return data.decode('utf-16-be')
    if data.startswith(b'\xef\xbb\xbf'):
        return data.decode('utf-8-sig')
    # UTF-16 LE 无 BOM：如 "<html" -> 3c 00 68 00 ...
    if len(data) >= 4 and data[1] == 0 and data[3] == 0:
        try:
            return data.decode('utf-16-le')
        except UnicodeDecodeError:
            pass
    if len(data) >= 4 and data[0] == 0 and data[2] == 0:
        try:
            return data.decode('utf-16-be')
        except UnicodeDecodeError:
            pass
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        pass
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        pass
    try:
        return data.decode('gb18030')
    except UnicodeDecodeError:
        pass
    return data.decode('utf-8', errors='replace')


def save_uploaded_file(file, upload_folder, allowed_extensions=None):
    """保存上传的文件
    
    Args:
        file: 文件对象
        upload_folder: 上传文件夹路径
        allowed_extensions: 允许的扩展名集合，如果为None则使用默认的ALLOWED_EXTENSIONS
    
    Returns:
        tuple: (unique_filename, filepath) 或 (None, None) 如果失败
    """
    try:
        if not file:
            logger.warning("save_uploaded_file: file对象为空")
            return None, None
        
        if not file.filename:
            logger.warning("save_uploaded_file: 文件名为空")
            return None, None
        
        if allowed_extensions is None:
            allowed_extensions = ALLOWED_EXTENSIONS
        
        if not allowed_file(file.filename, allowed_extensions):
            file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else '无扩展名'
            logger.warning(f"save_uploaded_file: 不支持的文件格式 - {file.filename}, 扩展名: {file_ext}, 允许的格式: {allowed_extensions}")
            return None, None
        
        # 仅保留扩展名，使用 uuid 生成短且唯一的文件名，避免长名与中文编码问题
        original_filename = file.filename
        ext = original_filename.rsplit('.', 1)[1].lower().strip() if '.' in original_filename else 'bin'
        ext = ext.replace('\ufeff', '').replace('\u200b', '').strip() or 'bin'
        if ext not in allowed_extensions:
            ext = list(allowed_extensions)[0]  # 回退到允许的任一扩展名
        unique_filename = uuid.uuid4().hex + '.' + ext
        filepath = os.path.join(upload_folder, unique_filename)
        
        # 确保上传目录存在
        if not os.path.exists(upload_folder):
            os.makedirs(upload_folder)
        
        file.save(filepath)
        
        # 验证文件是否真的保存成功
        if os.path.exists(filepath):
            return unique_filename, filepath
        else:
            logger.error(f"文件保存后验证失败: 文件不存在于 {filepath}")
            return None, None
    except Exception as e:
        logger.error(f"保存文件失败: {str(e)}, 文件名: {file.filename if file else 'None'}", exc_info=True)
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


def extract_useful_text_from_html(html_content):
    """解析HTML，保留主要可读文本（去掉脚本/样式/导航），尽量按段落输出。"""
    try:
        soup = BeautifulSoup(html_content or '', 'lxml')
        # 去除明显无用的标签
        for tag in soup(['script', 'style', 'noscript', 'template']):
            tag.decompose()
        for tag in soup.find_all(True):
            # 删除常见导航/页脚/广告区域（通过tag名粗略过滤）
            if tag.name in ['header', 'footer', 'nav', 'aside']:
                tag.decompose()
        # 提取纯文本，保留换行以形成段落
        raw_text = soup.get_text('\n', strip=True)
        # 归一化空白与段落：
        lines = [ln.strip() for ln in raw_text.split('\n')]
        lines = [ln for ln in lines if ln]  # 去掉空行
        # 过滤纯符号/过短噪声，但不过度删减
        filtered = []
        for ln in lines:
            # 去掉只有标点或长度极短的行，但保留标题等短句
            if len(ln) == 1 and not ln.isalnum():
                continue
            filtered.append(ln)
        # 合并相邻重复行，避免模板重复
        merged = []
        prev = None
        for ln in filtered:
            if ln != prev:
                merged.append(ln)
            prev = ln
        # 限制总长度，避免提示词过长
        text = '\n'.join(merged)
        if len(text) > 20000:
            text = text[:20000]
        return text
    except Exception:
        try:
            return BeautifulSoup(html_content or '', 'lxml').get_text('\n', strip=True)
        except Exception:
            return ''

