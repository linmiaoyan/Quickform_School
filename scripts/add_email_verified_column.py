#!/usr/bin/env python3
"""
一次性脚本：为 user 表添加 email_verified 列（若不存在）。
用于在未重启应用或自动迁移未执行时修复 500 错误。
使用方式：
  - MySQL：在项目根目录执行
    python scripts/add_email_verified_column.py
  - 或直接在 MySQL 客户端执行：
    ALTER TABLE user ADD COLUMN email_verified TINYINT(1) DEFAULT 1;
    UPDATE user SET email_verified = 1 WHERE email_verified IS NULL;
"""
import os
import sys

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    from dotenv import load_dotenv
    load_dotenv()
    use_mysql = bool(os.getenv('MYSQL_HOST') and os.getenv('MYSQL_USER') and os.getenv('MYSQL_PASSWORD'))
    if use_mysql:
        import pymysql
        db_name = (os.getenv('MYSQL_DATABASE') or 'quickform').strip() or 'quickform'
        if not db_name:
            print("错误：未设置 MYSQL_DATABASE，请在 .env 中配置或使用默认数据库名 quickform")
            return
        conn = pymysql.connect(
            host=os.getenv('MYSQL_HOST'),
            user=os.getenv('MYSQL_USER'),
            password=os.getenv('MYSQL_PASSWORD'),
            database=db_name,
            charset='utf8mb4'
        )
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM information_schema.COLUMNS 
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'user' AND COLUMN_NAME = 'email_verified'
                """, (db_name,))
                if cur.fetchone()[0] == 0:
                    cur.execute("ALTER TABLE user ADD COLUMN email_verified TINYINT(1) DEFAULT 1")
                    cur.execute("UPDATE user SET email_verified = 1 WHERE email_verified IS NULL")
                    conn.commit()
                    print("已为 user 表添加 email_verified 列。")
                else:
                    print("user 表已存在 email_verified 列，无需执行。")
        finally:
            conn.close()
    else:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'instance', 'quickform.db')
        if not os.path.exists(db_path):
            db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'quickform.db')
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute("PRAGMA table_info(user)")
            cols = [row[1] for row in cur.fetchall()]
            if 'email_verified' not in cols:
                conn.execute("ALTER TABLE user ADD COLUMN email_verified BOOLEAN DEFAULT 1")
                conn.execute("UPDATE user SET email_verified = 1 WHERE email_verified IS NULL")
                conn.commit()
                print("已为 user 表添加 email_verified 列。")
            else:
                print("user 表已存在 email_verified 列，无需执行。")
        finally:
            conn.close()

if __name__ == '__main__':
    main()
