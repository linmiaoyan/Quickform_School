"""将 ai_config 中历史明文密钥迁移为加密存储。"""
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def build_db_url() -> str:
    explicit = (os.getenv('DATABASE_URL') or '').strip()
    if explicit:
        return explicit
    host = (os.getenv('POSTGRES_HOST') or os.getenv('PGHOST') or 'localhost').strip()
    port = (os.getenv('POSTGRES_PORT') or os.getenv('PGPORT') or '5432').strip()
    user = (os.getenv('POSTGRES_USER') or os.getenv('PGUSER') or 'postgres').strip()
    password = os.getenv('POSTGRES_PASSWORD') or os.getenv('PGPASSWORD') or ''
    database = (os.getenv('POSTGRES_DB') or os.getenv('PGDATABASE') or 'quickform').strip()
    auth = user if password == '' else f"{user}:{password}"
    return f"postgresql+psycopg://{auth}@{host}:{port}/{database}"


def main() -> int:
    load_dotenv()
    # 兼容直接运行 scripts/*.py
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    from core.models import AIConfig  # pylint: disable=import-error
    from core.secret_store import encrypt_ai_config_inplace, AI_SECRET_FIELDS  # pylint: disable=import-error

    engine = create_engine(build_db_url())
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        rows = db.query(AIConfig).all()
        total = 0
        changed = 0
        for row in rows:
            total += 1
            before = {k: (getattr(row, k, '') or '') for k in AI_SECRET_FIELDS}
            encrypt_ai_config_inplace(row)
            after = {k: (getattr(row, k, '') or '') for k in AI_SECRET_FIELDS}
            if before != after:
                changed += 1
        db.commit()
        print(f"扫描 ai_config 记录 {total} 条，已加密更新 {changed} 条。")
        return 0
    except Exception as e:
        db.rollback()
        print(f"迁移失败: {e}")
        return 1
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
