"""快速检查 submission 表的数据量（PostgreSQL-only）。"""
import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
from core.db import build_database_url

# 加载环境变量
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env_path = os.path.join(project_root, '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    load_dotenv()

print("=" * 60)
print("检查submission表数据量")
print("=" * 60)

print("\n【PostgreSQL数据库】")
db_url = build_database_url()
print(f"连接: {db_url.split('@')[-1] if '@' in db_url else db_url}")
try:
    engine = create_engine(db_url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()

    total_count = db.execute(text("SELECT COUNT(*) FROM submission")).fetchone()[0]
    print(f"总记录数: {total_count:,}")

    task_stats = db.execute(text("""
        SELECT task_id, COUNT(*) as count
        FROM submission
        GROUP BY task_id
        ORDER BY count DESC
        LIMIT 10
    """)).fetchall()

    if task_stats:
        print("\n前10个任务的提交数量:")
        for task_id, count in task_stats:
            print(f"  任务ID {task_id}: {count:,} 条")

    orphan_count = db.execute(text("""
        SELECT COUNT(*)
        FROM submission s
        LEFT JOIN task t ON s.task_id = t.id
        WHERE t.id IS NULL
    """)).fetchone()[0]
    if orphan_count > 0:
        print(f"\n⚠️  孤立记录（任务已删除）: {orphan_count:,} 条")
finally:
    try:
        db.close()
    except Exception:
        pass

print("\n" + "=" * 60)
