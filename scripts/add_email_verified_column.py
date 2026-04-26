#!/usr/bin/env python3
"""
一次性脚本：为 user 表添加 email_verified 列（若不存在）。
用于在未重启应用或自动迁移未执行时修复 500 错误。
使用方式：
  - PostgreSQL：在项目根目录执行
    python scripts/add_email_verified_column.py
  - 或直接在 PostgreSQL 客户端执行：
    ALTER TABLE "user" ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT TRUE;
    UPDATE "user" SET email_verified = TRUE WHERE email_verified IS NULL;
"""
import os
import sys

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    from dotenv import load_dotenv
    load_dotenv()
    import psycopg

    database_url = (os.getenv('DATABASE_URL') or '').strip()
    if not database_url:
        host = (os.getenv('POSTGRES_HOST') or os.getenv('PGHOST') or 'localhost').strip()
        port = (os.getenv('POSTGRES_PORT') or os.getenv('PGPORT') or '5432').strip()
        user = (os.getenv('POSTGRES_USER') or os.getenv('PGUSER') or 'postgres').strip()
        password = os.getenv('POSTGRES_PASSWORD') or os.getenv('PGPASSWORD') or ''
        database = (os.getenv('POSTGRES_DB') or os.getenv('PGDATABASE') or 'quickform').strip()
        auth = user if password == '' else f"{user}:{password}"
        database_url = f"postgresql://{auth}@{host}:{port}/{database}"

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT TRUE;')
            cur.execute('UPDATE "user" SET email_verified = TRUE WHERE email_verified IS NULL;')
        conn.commit()
    print('已确保 user 表存在 email_verified 列（PostgreSQL）。')

if __name__ == '__main__':
    main()
