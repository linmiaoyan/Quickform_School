"""管理员：任务标题文件缓存、词云与轻量分类（供 blueprint 与后台接口使用）。"""
import json
import os
import re
from datetime import datetime

from .models import Task

# 说明：缓存为运行时文件，不纳入 git；默认过滤过短/低信息标题（见 ADMIN_TASK_TITLES_MIN_LEN）。
# 用当前包路径推导仓库根目录侧的运行时目录（不依赖 blueprint 内较晚定义的 APP_ROOT）。
_ADMIN_CACHE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'runtime_cache'))
_ADMIN_TASK_TITLES_CACHE_PATH = os.path.join(_ADMIN_CACHE_DIR, 'admin_task_titles_cache.json')
ADMIN_TASK_TITLES_MIN_LEN = int(os.getenv('ADMIN_TASK_TITLES_MIN_LEN', '4') or '4')
ADMIN_TASK_TITLES_CACHE_MAX = int(os.getenv('ADMIN_TASK_TITLES_CACHE_MAX', '200000') or '200000')


def _ensure_admin_cache_dir():
    try:
        os.makedirs(_ADMIN_CACHE_DIR, exist_ok=True)
    except Exception:
        pass


def _clean_task_title(s: str) -> str:
    try:
        t = (s or '').strip()
        if not t:
            return ''
        t = re.sub(r'\s+', ' ', t)
        return t[:160]
    except Exception:
        return ''


def _is_low_info_title(t: str, min_len: int = None) -> bool:
    try:
        ml = int(min_len) if min_len is not None else int(ADMIN_TASK_TITLES_MIN_LEN or 4)
    except Exception:
        ml = 4
    if not t:
        return True
    if len(t) < ml:
        return True
    if re.fullmatch(r'[\W_]+', t):
        return True
    if re.fullmatch(r'\d+', t):
        return True
    return False


def _load_task_titles_cache():
    try:
        if not os.path.exists(_ADMIN_TASK_TITLES_CACHE_PATH):
            return None, None
        with open(_ADMIN_TASK_TITLES_CACHE_PATH, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return None, None
        titles = payload.get('titles') or []
        if not isinstance(titles, list):
            titles = []
        return payload, titles
    except Exception:
        return None, None


def _rebuild_task_titles_cache(db, force: bool = False):
    _ensure_admin_cache_dir()
    if not force:
        payload, titles = _load_task_titles_cache()
        if payload and isinstance(titles, list) and titles:
            return payload

    titles = []
    total = 0
    skipped = 0
    try:
        q = db.query(Task.title)
        for r in q:
            total += 1
            raw = r[0] if isinstance(r, (list, tuple)) else getattr(r, 'title', None)
            t = _clean_task_title(raw or '')
            if _is_low_info_title(t):
                skipped += 1
                continue
            titles.append(t)
            if len(titles) >= int(ADMIN_TASK_TITLES_CACHE_MAX or 200000):
                break
    except Exception:
        pass

    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'total_tasks_scanned': int(total),
        'min_len': int(ADMIN_TASK_TITLES_MIN_LEN or 4),
        'skipped_low_info': int(skipped),
        'titles_count': int(len(titles)),
        'titles': titles,
    }
    try:
        tmp_path = _ADMIN_TASK_TITLES_CACHE_PATH + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, _ADMIN_TASK_TITLES_CACHE_PATH)
    except Exception:
        pass
    return payload


_TITLE_STOPWORDS = {
    '的', '了', '和', '与', '及', '或', '在', '对', '为', '及其', '以及', '一个', '一种', '如何', '为什么',
    '练习', '测试', '测验', '作业', '答案', '题', '题目', '试题', '试卷', '单元', '期中', '期末',
    '（', '）', '(', ')', '-', '_', '/', '\\', '|', ':', '：', ',', '，', '.', '。', '、',
}


def _tokenize_title_light(t: str):
    """零依赖轻量分词：抽取中文连续片段与英文/数字词。"""
    if not t:
        return []
    t = t.lower()
    zh = re.findall(r'[\u4e00-\u9fff]{2,}', t)
    en = re.findall(r'[a-z0-9]{3,}', t)
    toks = zh + en
    out = []
    for x in toks:
        x = x.strip()
        if not x:
            continue
        if x in _TITLE_STOPWORDS:
            continue
        out.append(x)
    return out


def _classify_title_light(t: str):
    """简单规则分类：根据关键词命中返回分类名。"""
    s = (t or '').lower()
    rules = [
        ('英语/外语', ['英语', 'english', '听力', '口语', '阅读', '作文', '词汇', '语法']),
        ('理科/数学', ['数学', '几何', '代数', '函数', '方程', '数列', '概率', '统计']),
        ('理科/物理', ['物理', '力学', '电学', '磁', '光学', '热学']),
        ('理科/化学', ['化学', '酸', '碱', '氧化', '还原', '离子', '元素', '反应', '溶液']),
        ('文科/语文', ['语文', '阅读理解', '古诗', '文言', '作文', '写作', '修辞']),
        ('文科/历史', ['历史', '朝代', '战争', '革命', '史']),
        ('文科/地理', ['地理', '气候', '地形', '人口', '城市', '地图']),
        ('生物/科学', ['生物', '细胞', '遗传', '生态', '科学', '实验']),
        ('通知/活动', ['通知', '报名', '活动', '会议', '安排']),
        ('数据/隐私', ['数据', '隐私', '保护', '协议', '条款']),
    ]
    for cat, keys in rules:
        for k in keys:
            if k in s:
                return cat
    if any(x in s for x in ['测试', '测验', '试卷', '试题', '题库']):
        return '测评/试卷'
    return '其他'


def _analyze_titles_light(titles, top_n: int = 60):
    """返回：词频 topN + 分类计数 + 质量过滤信息。"""
    from collections import Counter, defaultdict

    word_counter = Counter()
    cat_counter = Counter()
    examples_by_cat = defaultdict(list)
    for t in titles:
        cat = _classify_title_light(t)
        cat_counter[cat] += 1
        if len(examples_by_cat[cat]) < 3:
            examples_by_cat[cat].append(t)
        for tok in _tokenize_title_light(t):
            word_counter[tok] += 1
    top_words = [{'word': w, 'count': int(c)} for w, c in word_counter.most_common(max(5, int(top_n or 60)))]
    cats = [{'category': k, 'count': int(v), 'examples': examples_by_cat.get(k, [])} for k, v in cat_counter.most_common()]
    return top_words, cats


def _analyze_task_titles(titles, top_n: int = 60):
    """兼容别名：历史实现中路由调用了 _analyze_task_titles。"""
    return _analyze_titles_light(titles, top_n=top_n)
