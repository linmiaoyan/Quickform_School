"""
登录防暴力破解 / 撞库：按客户端 IP（及可选「IP+账号」）在内存中计数。

说明：
- 多进程部署（如 gunicorn workers）时每个进程内存独立，防护被稀释；生产建议前置 WAF/网关限流或 Redis。
- 信任链与 blueprint 其它限流一致：优先 X-Forwarded-For 第一段（需反向代理正确配置）。
"""
from __future__ import annotations

import threading
import time
from collections import deque

# ---------- 防撞库 / 暴力破解参数（固定写在代码中，可按需改数值后部署）----------
# 统计窗口（秒）：滑动窗口内统计失败次数
FAIL_WINDOW_SEC = 900  # 15 分钟
# 每 IP 在窗口内允许失败次数，超过则整 IP 锁定 LOCKOUT_SEC
MAX_FAILS_PER_IP = 40
# 超过阈值后的锁定时长（秒）
LOCKOUT_SEC = 900  # 15 分钟

# 同一 IP 对「同一登录名输入」的失败上限（与上面独立计数）
PAIR_WINDOW_SEC = 900  # 与 FAIL 窗口一致
MAX_FAILS_PER_PAIR = 15
PAIR_LOCKOUT_SEC = 900

_lock = threading.Lock()
# ip -> deque[timestamp]
_ip_fail_times: dict[str, deque] = {}
# ip -> lockout_until_ts
_ip_lockout_until: dict[str, float] = {}
# "ip\0username_norm" -> deque
_pair_fail_times: dict[str, deque] = {}
_pair_lockout_until: dict[str, float] = {}


def _client_ip(request) -> str:
    xff = (request.headers.get('X-Forwarded-For') or '').strip()
    if xff:
        return xff.split(',')[0].strip() or 'unknown'
    return (request.remote_addr or '').strip() or 'unknown'


def _pair_key(ip: str, username: str) -> str:
    u = (username or '').strip()[:200].lower()
    return f'{ip}\x00{u}'


def _prune_deque(dq: deque, now: float, window: float) -> None:
    while dq and now - dq[0] > window:
        dq.popleft()


def _locked_until(now: float, until_map: dict, key: str) -> float:
    t = until_map.get(key)
    if t is None or t <= now:
        if key in until_map:
            del until_map[key]
        return 0.0
    return t


def login_blocked(request, username: str | None = None) -> tuple[bool, int]:
    """
    是否应拒绝本次登录尝试（在验证密码之前调用）。
    返回 (blocked, retry_after_seconds)。
    """
    ip = _client_ip(request)
    now = time.time()
    with _lock:
        ip_until = _locked_until(now, _ip_lockout_until, ip)
        if ip_until:
            return True, max(1, int(ip_until - now) + 1)
        if username is not None:
            pk = _pair_key(ip, username)
            p_until = _locked_until(now, _pair_lockout_until, pk)
            if p_until:
                return True, max(1, int(p_until - now) + 1)
    return False, 0


def record_login_failure(request, username: str | None = None) -> None:
    """密码错误或账号不存在时调用，累计失败并可能在超阈值时锁定。"""
    ip = _client_ip(request)
    now = time.time()
    uname = (username or '').strip()

    with _lock:
        # ---- IP ----
        dq = _ip_fail_times.setdefault(ip, deque())
        _prune_deque(dq, now, FAIL_WINDOW_SEC)
        dq.append(now)
        if len(dq) > MAX_FAILS_PER_IP:
            _ip_lockout_until[ip] = now + LOCKOUT_SEC
            dq.clear()

        # ---- IP + 登录名 ----
        if uname:
            pk = _pair_key(ip, uname)
            pdq = _pair_fail_times.setdefault(pk, deque())
            _prune_deque(pdq, now, PAIR_WINDOW_SEC)
            pdq.append(now)
            if len(pdq) > MAX_FAILS_PER_PAIR:
                _pair_lockout_until[pk] = now + PAIR_LOCKOUT_SEC
                pdq.clear()


def clear_login_throttle(request, username: str | None = None) -> None:
    """登录成功时清理该 IP 的计数与锁定（改善正常用户出 NAT 后的体验）。"""
    ip = _client_ip(request)
    with _lock:
        _ip_fail_times.pop(ip, None)
        _ip_lockout_until.pop(ip, None)
        if username is not None:
            _pair_fail_times.pop(_pair_key(ip, username), None)
            _pair_lockout_until.pop(_pair_key(ip, username), None)
        # 顺带清理该 IP 下所有 pair 记录（避免同 NAT 下他人试错牵连）
        prefix = ip + '\x00'
        for k in list(_pair_fail_times.keys()):
            if k.startswith(prefix):
                _pair_fail_times.pop(k, None)
        for k in list(_pair_lockout_until.keys()):
            if k.startswith(prefix):
                _pair_lockout_until.pop(k, None)
