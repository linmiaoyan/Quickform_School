# QuickForm（校园版 / PostgreSQL-only）

QuickForm 是一个用于**表单数据收集 + AI 分析**的 Flask 应用。本仓库版本为 **PostgreSQL-only**（不再支持 MySQL/SQLite）。

## 快速开始（服务器部署）

### 1) 准备 PostgreSQL

PostgreSQL 安装：到 PostgreSQL 官网下载并安装对应系统版本即可。

在 PostgreSQL 中创建用户与数据库（示例）：

```sql
CREATE USER quickform WITH PASSWORD '你的数据库密码';
CREATE DATABASE quickform OWNER quickform;
```

### 2) 配置 `.env`

在仓库根目录（与 `app.py` 同级）创建 `.env`。

推荐做法：先复制模板再修改：

- Windows：
  - `copy example.env .env`
- Linux/macOS：
  - `cp example.env .env`

最少需要确认两项：

- `SECRET_KEY`: 强随机字符串（必须配置）
- `DATABASE_URL`: PostgreSQL 连接串（优先使用）

示例：

```env
SECRET_KEY=请填强随机值
DATABASE_URL=postgresql+psycopg://quickform:你的数据库密码@127.0.0.1:5432/quickform
```

如果你要让同网段通过**内网 IP/域名**访问（不使用 Nginx），确保监听地址不是 127.0.0.1：

```env
FLASK_HOST=0.0.0.0
FLASK_PORT=80
```

### 3) 安装依赖并启动

```bash
python -m pip install -r requirements.txt
python app.py
```

健康检查：

- `http://127.0.0.1/ping` → `pong`

## 默认管理员账号

首次启动会尝试自动创建/确保管理员账号存在：

- 用户名：`wzkjgz`
- 密码：`wzkjgz123!`

建议首次登录后立刻修改密码。

## 任务迁移（在线版 → 校园版/教师版）

校园版支持从在线版（默认 `https://quickform.cn`）导入任务：

- **方式 1**：获取全部任务列表后选择某个任务导入
- **方式 2**：直接粘贴任务 API 地址（形如 `https://quickform.cn/api/<apiid>`）导入

导入规则：

- **APIID 不存在**：沿用在线版的 APIID
- **APIID 已存在**：自动重新生成新的 APIID
- 会下载任务的 **HTML 附件** 并将其中的接口地址从在线版改写为本系统的接口地址

入口：

- 仪表盘右上角用户菜单的「导入任务」
- 或直接访问：`/task/migration`

## 文档索引

- CLI 接口：`docs/CLI接口说明.md`
- Nginx 反向代理示例（可选）：`docs/nginx.md`

