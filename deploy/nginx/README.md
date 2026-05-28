# Nginx + HTTPS 部署（腾讯云 SSL 证书）

本目录提供**固定路径**与**一键脚本**，便于你在服务器上只复制公钥/私钥即可启用 HTTPS。

## 目录与路径约定（Ubuntu / Debian）

| 用途 | 服务器路径 |
|------|------------|
| SSL 证书（腾讯云 Nginx 包里的 `.crt` / `_bundle.crt`） | `/etc/nginx/ssl/quickform/fullchain.crt` |
| 私钥（`.key`） | `/etc/nginx/ssl/quickform/private.key` |
| 站点配置 | `/etc/nginx/sites-available/quickform.conf` → `sites-enabled/` |
| 应用代码（示例） | `/opt/quickform` |
| 应用内网端口（Waitress） | `127.0.0.1:5000` |

仓库内对应占位目录（**勿把真实私钥提交到 Git**）：

- `deploy/nginx/ssl/` — 仅说明用，见其中 `README.txt`

## 快速部署（推荐）

在**项目根目录**执行（需 root 或 sudo）：

```bash
export QUICKFORM_DOMAIN=你的域名.com
export QUICKFORM_APP_ROOT=/opt/quickform
sudo -E bash scripts/deploy/install_nginx_ubuntu.sh
```

然后把腾讯云下载的 Nginx 证书文件复制到服务器：

```bash
sudo cp 你的证书_bundle.crt /etc/nginx/ssl/quickform/fullchain.crt
sudo cp 你的证书.key           /etc/nginx/ssl/quickform/private.key
sudo chmod 600 /etc/nginx/ssl/quickform/private.key
sudo nginx -t && sudo systemctl reload nginx
```

## 应用 `.env`（与 Nginx 配合）

应用只监听本机，由 Nginx 终结 TLS：

```env
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
PUBLIC_BASE_URL=https://你的域名.com
PREFERRED_URL_SCHEME=https
SESSION_COOKIE_SECURE=true
REMEMBER_COOKIE_SECURE=true
```

启动应用（生产建议 Waitress）：

```bash
cd /opt/quickform
python3 -m pip install -r requirements.txt
python3 app_waitress.py
```

或使用 systemd：`deploy/systemd/quickform.service`（复制到 `/etc/systemd/system/` 后 `systemctl enable --now quickform`）。

## 手动替换模板变量

若不用脚本，可复制模板并替换占位符：

```bash
sudo cp deploy/nginx/quickform.conf.template /etc/nginx/sites-available/quickform.conf
# 编辑文件，将 __SERVER_NAME__、__SSL_*__、__APP_ROOT__、__UPSTREAM_PORT__ 改为实际值
sudo ln -sf /etc/nginx/sites-available/quickform.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 腾讯云安全组

- 入站放行 **80**、**443**
- 不要将 **5000** 对公网开放（仅本机 Nginx 反代）

## 无 Nginx 时

见根目录 `docs/nginx.md` 或使用腾讯云 CLB 在负载均衡层挂证书。
