# QuickForm（Windows 服务器）PostgreSQL 本机部署指南

本指南适用于：你是 Windows 服务器管理员，希望在**同一台服务器**上安装并使用 PostgreSQL 作为 QuickForm 的数据库（PostgreSQL-only 版本）。

---

## 1) 安装 PostgreSQL（本机）

1. 使用 PostgreSQL 官方 Windows 安装包安装（一路下一步即可）。
2. **务必记住你设置的 `postgres` 管理员密码**。
3. 安装完成后确认服务在跑：
   - 打开 `services.msc`
   - 找到 `postgresql-x64-xx`（版本号可能不同）
   - 状态应为“正在运行”

---

## 2) 创建数据库与用户

使用安装自带的 **SQL Shell (psql)** 或命令行 `psql`，执行（示例）：

```sql
CREATE USER quickform WITH PASSWORD '强密码';
CREATE DATABASE quickform OWNER quickform;
```

如果你已经创建过 `quickform` 用户，想要**修改密码**：

```sql
ALTER USER quickform WITH PASSWORD '新密码';
```

---

## 3) 配置 QuickForm 的 `.env`

在仓库根目录（与 `app.py` 同级）新建 `.env`，至少写入：

```env
SECRET_KEY=请填强随机值
DATABASE_URL=postgresql+psycopg://quickform:强密码@127.0.0.1:5432/quickform
```

如果你先在内网跑 HTTP，再用 Nginx/网关做 HTTPS，通常还会加：

```env
SESSION_COOKIE_SECURE=false
REMEMBER_COOKIE_SECURE=false
FLASK_HOST=0.0.0.0
FLASK_DEBUG=false
```

---

## 4) 安装 Python 依赖并启动

在项目目录执行：

```bat
python -m pip install -r requirements.txt
python app.py
```

健康检查（本机）：

- 浏览器访问 `http://127.0.0.1/ping` 应返回 `pong`

