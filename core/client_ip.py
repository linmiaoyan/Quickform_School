"""
经反向代理（如 Nginx）时，从请求中解析「用于限流与审计」的客户端 IP。

说明（与 Nginx proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for 配合）：
- 浏览器直连 Nginx 时，通常只有一个地址，即真实访客。
- 若请求里带有伪造的 X-Forwarded-For，常见形态为「伪造链, 直连 Nginx 的 IP」，
  此时应取链中最后一个由 Nginx 追加的地址作为可信客户端（默认策略）。
- 仅当应用进程只对受信反代监听、不对公网直开端口时，才可信任上述逻辑；
  若对公网直开，客户端可冒充 X-Real-IP / XFF，需网络层隔离。

环境变量（可选）：
- QUICKFORM_CLIENT_IP_XFF_PICK=last   默认，取 XFF 逗号分隔的最后一段（推荐）
- QUICKFORM_CLIENT_IP_XFF_PICK=first  取第一段（仅当你确信入口已剥离客户端伪造 XFF 时使用）
"""

import os


def _normalize_ip_token(raw: str) -> str:
    if not raw:
        return ''
    s = raw.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    s = s.strip('[]')
    if not s:
        return ''
    if '%' in s and ':' in s:
        s = s.split('%', 1)[0]
    return s


def get_request_client_ip(req, max_len: int = 100) -> str:
    """从 Flask/Werkzeug request 解析客户端 IP，供限流、ApiAccessLog、审计日志使用。"""
    if req is None:
        return 'unknown'

    pick = (os.getenv('QUICKFORM_CLIENT_IP_XFF_PICK') or 'last').strip().lower()
    use_first = pick in ('first', 'left', '1')

    xff = req.headers.get('X-Forwarded-For') or req.environ.get('HTTP_X_FORWARDED_FOR')
    if xff:
        parts = [_normalize_ip_token(p) for p in xff.split(',')]
        parts = [p for p in parts if p]
        if parts:
            token = parts[0] if use_first else parts[-1]
            return token[:max_len]

    xr = req.headers.get('X-Real-IP') or req.environ.get('HTTP_X_REAL_IP')
    if xr:
        token = _normalize_ip_token(xr)
        if token:
            return token[:max_len]

    ra = (getattr(req, 'remote_addr', None) or '').strip()
    if ra:
        return ra[:max_len]
    return 'unknown'
