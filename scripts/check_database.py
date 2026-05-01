"""数据库检查和修复脚本"""
import os
import sys

print("=" * 60)
print("QuickForm 数据库和架构检查")
print("=" * 60)
print()

# 1. 检查环境变量
print("【1】检查 .env 配置")
print("-" * 40)
if os.path.exists('.env'):
    print("✓ .env 文件存在")
    with open('.env', 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                if 'PASSWORD' in line or 'SECRET_KEY' in line:
                    key = line.split('=')[0]
                    print(f"  {key}=***")
                else:
                    print(f"  {line}")
else:
    print("⚠ .env 文件不存在（将使用默认配置）")
print()

# 2. 检查数据库类型配置
print("【2】数据库配置")
print("-" * 40)
from dotenv import load_dotenv
load_dotenv()

db_url = (os.getenv('DATABASE_URL') or '').strip()
if not db_url:
    host = (os.getenv('POSTGRES_HOST') or os.getenv('PGHOST') or 'localhost').strip()
    port = (os.getenv('POSTGRES_PORT') or os.getenv('PGPORT') or '5432').strip()
    user = (os.getenv('POSTGRES_USER') or os.getenv('PGUSER') or 'postgres').strip()
    password = os.getenv('POSTGRES_PASSWORD') or os.getenv('PGPASSWORD') or ''
    database = (os.getenv('POSTGRES_DB') or os.getenv('PGDATABASE') or 'quickform').strip()
    auth = user if password == '' else f"{user}:{password}"
    db_url = f"postgresql+psycopg://{auth}@{host}:{port}/{database}"

print("数据库类型: postgres")
print("数据库连接: 使用 DATABASE_URL / POSTGRES_*（敏感信息已隐藏）")

print()

# 3. 检查项目架构
print("【3】检查项目架构和导入")
print("-" * 40)

required_files = [
    'app.py',
    'blueprint.py',
    'models.py',
    'ai_service.py',
    'file_service.py',
    'report_service.py',
    'utils.py',
    'requirements.txt'
]

missing_files = []
for file in required_files:
    if os.path.exists(file):
        print(f"✓ {file}")
    else:
        print(f"✗ {file} - 缺失")
        missing_files.append(file)

if missing_files:
    print(f"\n⚠ 缺失 {len(missing_files)} 个核心文件!")
else:
    print("\n✓ 所有核心文件完整")

print()

# 4. 测试Python导入
print("【4】测试Python模块导入")
print("-" * 40)

try:
    from models import Base, User, Task, Submission, AIConfig
    print("✓ models.py 导入成功")
except Exception as e:
    print(f"✗ models.py 导入失败: {e}")

try:
    from blueprint import quickform_bp, init_quickform, SessionLocal
    print("✓ blueprint.py 导入成功")
except Exception as e:
    print(f"✗ blueprint.py 导入失败: {e}")

try:
    from ai_service import call_ai_model, generate_analysis_prompt
    print("✓ ai_service.py 导入成功")
except Exception as e:
    print(f"✗ ai_service.py 导入失败: {e}")

try:
    from file_service import save_uploaded_file, read_file_content
    print("✓ file_service.py 导入成功")
except Exception as e:
    print(f"✗ file_service.py 导入失败: {e}")

try:
    from report_service import save_analysis_report, generate_report_image
    print("✓ report_service.py 导入成功")
except Exception as e:
    print(f"✗ report_service.py 导入失败: {e}")

print()

# 5. 检查依赖
print("【5】检查Python依赖")
print("-" * 40)

required_packages = [
    'flask',
    'flask_login',
    'flask_bcrypt',
    'sqlalchemy',
    'python-dotenv',
    'pandas',
    'openpyxl',
    'matplotlib'
]

missing_packages = []
for package in required_packages:
    try:
        __import__(package.replace('-', '_'))
        print(f"✓ {package}")
    except ImportError:
        print(f"✗ {package} - 未安装")
        missing_packages.append(package)

if missing_packages:
    print(f"\n⚠ 缺失 {len(missing_packages)} 个依赖包")
    print("安装命令: pip install -r requirements.txt")
else:
    print("\n✓ 所有依赖包已安装")

print()

# 6. 总结和建议
print("=" * 60)
print("【总结和建议】")
print("=" * 60)

print()
print("✓ 数据库使用 PostgreSQL（建议通过 DATABASE_URL 或 POSTGRES_* 配置）")
print()
print("启动应用:")
print("  python3 app.py")
print()
print("健康检查:")
print("  curl http://localhost/ping  # 返回 pong")

print()
print("=" * 60)
