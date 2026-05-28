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

- Windows：`copy example.env .env`
- Linux/macOS：`cp example.env .env`

`example.env` 里用 **`>>>` 注释框**标出了部署后**需要按环境填写或核对**的配置项，请对照仓库内文件逐项处理，不要只抄 `SECRET_KEY` 和数据库两项。

#### 必须正确配置（否则会无法运行、无法发信或链接/HTTPS 异常）

| 变量 | 说明 |
|------|------|
| `SECRET_KEY` | 强随机字符串；勿使用占位符。 |
| `DATABASE_URL` | PostgreSQL 连接串（也可改用 `POSTGRES_*` 分项，见 `example.env`）。 |
| `PUBLIC_BASE_URL` | 用户访问的站点根地址（含协议与端口），**反代 / HTTPS 时必须设置**，避免混合内容或外链错误。 |
| `PREFERRED_URL_SCHEME` | 与对外协议一致，如 `https`。 |
| `SESSION_COOKIE_SECURE` / `REMEMBER_COOKIE_SECURE` | HTTPS 下一般为 `true`；纯 HTTP 内网可为 `false`。 |
| `FLASK_HOST` / `FLASK_PORT` | 监听地址与端口；要让局域网直接访问应用时应用 `0.0.0.0` 等，不要长期只绑 `127.0.0.1` 却期望内网访问。 |
| `WAITRESS_THREADS` | 使用 `app_waitress.py` 时的线程数。 |
| `MAIL_SERVER` / `MAIL_PORT` / `MAIL_USE_TLS` | SMTP；端口与加密方式需与邮箱服务商说明一致。 |
| `MAIL_USERNAME` / `MAIL_PASSWORD` / `MAIL_DEFAULT_SENDER` | 发信账号与授权码（或厂商要求的方式）；**不配则邮件验证码等功能不可用**。 |

#### 建议配置

| 变量 | 说明 |
|------|------|
| `CHAT_SERVER_API_TOKEN` | 硅基流动等：用户未在个人中心配置 API 时的默认 Token（一键内测）。 |
| `API_READ_CACHE_TTL` / `API_READ_CACHE_MAX_KEYS` | 只读 API 内存缓存，一般可保持默认。 |
| `DB_DELETE_BATCH_SIZE` / `DB_DELETE_MAX_RETRIES` / `DB_DELETE_RETRY_SLEEP` | 管理端批量清空等逻辑的分批与重试；`example.env` 已与代码默认一致，`cp example.env .env` 后**无需单独调整**即可用。仅在并发很高或大批量删除仍遇锁等待时再酌情调小批次或加大间隔。 |

最小可运行示例（仍需把密码、域名改成你的实际值）：

```env
SECRET_KEY=请填强随机值
DATABASE_URL=postgresql+psycopg://quickform:你的数据库密码@127.0.0.1:5432/quickform
PUBLIC_BASE_URL=https://你的站点根地址
PREFERRED_URL_SCHEME=https
SESSION_COOKIE_SECURE=true
REMEMBER_COOKIE_SECURE=true
```

若要让同网段通过**内网 IP** 直接访问应用（前面无 Nginx），监听需绑到所有网卡，例如：

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

- 用户名：`DEFAULT_ADMIN_USERNAME`（默认 `wst`）
- 密码：`DEFAULT_ADMIN_PASSWORD`（默认 `quickform`）

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
- Nginx / HTTPS（腾讯云证书路径与安装脚本）：`deploy/nginx/README.md`
- Nginx 简要说明：`docs/nginx.md`

