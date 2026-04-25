## PostgreSQL（小白快速搭建指南）

这份指南只讲“把 PostgreSQL 服务跑起来，并让 QuickForm 连上”，不需要你理解数据库原理。

### 方案 A（推荐）：Docker 一键启动（最省事）

1) 安装 Docker（桌面端或服务器均可）。

2) 在任意目录新建一个文件 `docker-compose.yml`，内容如下（用户名/密码/库名可按需改）：

```yaml
services:
  postgres:
    image: postgres:16
    container_name: quickform-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: quickform
    ports:
      - "5432:5432"
    volumes:
      - quickform_pgdata:/var/lib/postgresql/data

volumes:
  quickform_pgdata:
```

3) 启动：

```bash
docker compose up -d
```

4) 验证是否启动成功：

```bash
docker ps
```

看到 `quickform-postgres` 状态为 `Up` 即可。

5) 让 QuickForm 连接 PostgreSQL（推荐只配一个变量 `DATABASE_URL`）：

```bash
export DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/quickform"
```

如果 QuickForm 跑在另一台机器上，把 `127.0.0.1` 换成 PostgreSQL 所在机器的 IP 或域名即可。

> 注意：数据库密码不要用 `postgres` 这种弱密码，正式环境请换强密码，并限制网络访问（见下方“安全建议”）。

---

### 方案 B：本机安装（不想用 Docker）

#### Ubuntu/Debian

```bash
sudo apt-get update
sudo apt-get install -y postgresql
sudo systemctl enable --now postgresql
```

创建库与用户（下面示例用 `quickform/quickform`，你也可以自定义）：

```bash
sudo -u postgres psql -c "CREATE USER quickform WITH PASSWORD 'quickform';"
sudo -u postgres psql -c "CREATE DATABASE quickform OWNER quickform;"
```

QuickForm 连接：

```bash
export DATABASE_URL="postgresql+psycopg://quickform:quickform@127.0.0.1:5432/quickform"
```

#### macOS（Homebrew）

```bash
brew install postgresql@16
brew services start postgresql@16
createdb quickform
```

然后设置 `DATABASE_URL`（按你的用户名/密码调整）。

---

### 方案 C：云托管（不在自己服务器上装）

常见选择：Supabase / Neon / RDS / Cloud SQL 等。

你只需要拿到供应商给的连接串（通常长这样）：

```text
postgresql://USER:PASSWORD@HOST:5432/DBNAME
```

把它改成 SQLAlchemy 需要的形式（加上 `+psycopg`）：

```bash
export DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME"
```

---

### 安全建议（非常重要，尤其是校园版部署到公网）

- **不要把 5432 端口直接暴露到公网**。如果必须暴露，请至少：
  - 只允许 QuickForm 服务器 IP 访问（安全组/防火墙白名单）
  - 使用强密码
  - 最好启用 TLS（由云托管通常默认提供）
- QuickForm 和 PostgreSQL **放同一台服务器或同一内网**最稳。

---

### QuickForm 需要你额外安装什么？

- **部署/运维**：需要一个正在运行的 PostgreSQL 服务（Docker/本机/云）。
- **普通使用者（老师/学生）**：不需要安装任何数据库软件。
