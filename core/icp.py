"""ICP 备案号校验（页脚展示用）。"""
import re

# 例：浙ICP备2025205635号
ICP_RECORD_RE = re.compile(
    r'^[\u4e00-\u9fa5]{1,10}ICP备\d{6,14}号?$',
    re.UNICODE,
)


def normalize_icp_record(value: str) -> str:
    return (value or '').strip()


def is_valid_icp_record(value: str) -> bool:
    s = normalize_icp_record(value)
    if not s:
        return True
    return bool(ICP_RECORD_RE.match(s))
