"""将 ai_config 中历史明文密钥迁移为加密存储。"""
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def build_db_url() -> str:
    mysql_host = os.getenv('MYSQL_HOST', '')
    mysql_user = os.getenv('MYSQL_USER', '')
    mysql_password = os.getenv('MYSQL_PASSWORD', '')
    mysql_database = os.getenv('MYSQL_DATABASE', 'quickform')

    if os.getenv('DATABASE_TYPE'):
        db_type = os.getenv('DATABASE_TYPE', 'sqlite').lower()
    elif mysql_host and mysql_user and mysql_password:
        db_type = 'mysql'
    else:
        db_type = 'sqlite'

    if db_type == 'mysql':
        port = os.getenv('MYSQL_PORT', '3306')
        return f"mysql+pymysql://{mysql_user}:{mysql_password}@{mysql_host}:{port}/{mysql_database}?charset=utf8mb4"
    return "sqlite:///core/quickform.db"


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
