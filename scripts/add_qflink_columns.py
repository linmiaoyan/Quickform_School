#!/usr/bin/env python3
"""
一次性脚本：为 user 表添加 QFLink 相关列（若不存在）。

使用方式（项目根目录）：
  python scripts/add_qflink_columns.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_database_url() -> str:
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if database_url:
        return database_url
    host = (os.getenv("POSTGRES_HOST") or os.getenv("PGHOST") or "localhost").strip()
    port = (os.getenv("POSTGRES_PORT") or os.getenv("PGPORT") or "5432").strip()
    user = (os.getenv("POSTGRES_USER") or os.getenv("PGUSER") or "postgres").strip()
    password = os.getenv("POSTGRES_PASSWORD") or os.getenv("PGPASSWORD") or ""
    database = (os.getenv("POSTGRES_DB") or os.getenv("PGDATABASE") or "quickform").strip()
    auth = user if password == "" else f"{user}:{password}"
    return f"postgresql://{auth}@{host}:{port}/{database}"


def main():
    from dotenv import load_dotenv

    load_dotenv()
    import psycopg

    database_url = _get_database_url()
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS qflink_uid VARCHAR(128);')
            # UNIQUE 约束可能已存在或因重复数据失败，这里用 try 包裹
            try:
                cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_user_qflink_uid ON "user"(qflink_uid) WHERE qflink_uid IS NOT NULL;')
            except Exception:
                pass
            cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS qflink_only BOOLEAN DEFAULT FALSE;')
            cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS qflink_disabled BOOLEAN DEFAULT FALSE;')
        conn.commit()
    print("已确保 user 表存在 QFLink 相关列（PostgreSQL）。")


if __name__ == "__main__":
    main()

