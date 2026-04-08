"""生产启动入口（Windows 推荐）：使用 Waitress 代替 Flask 内置开发服务器。"""
import os
from waitress import serve
from app import app


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    threads = int(os.getenv("WAITRESS_THREADS", "8"))

    # 说明：生产环境建议通过 Nginx 反向代理到该端口。
    serve(app, host=host, port=port, threads=threads)
