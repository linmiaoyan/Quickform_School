"""生产启动入口（Waitress）。"""
import os
from waitress import serve
from app import app


def main():
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "80"))
    threads = int(os.getenv("WAITRESS_THREADS", "8"))
    serve(app, host=host, port=port, threads=threads)


if __name__ == "__main__":
    main()
