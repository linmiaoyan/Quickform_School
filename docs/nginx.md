# Nginx 反向代理 QuickForm

生产环境建议：**Nginx 终结 HTTPS**，应用在本机用 Waitress 跑 HTTP。

完整路径约定、腾讯云证书放置位置与安装脚本见：

- **[deploy/nginx/README.md](../deploy/nginx/README.md)**

## 一分钟流程

1. 应用 `.env`：`FLASK_HOST=127.0.0.1`、`FLASK_PORT=5000`、`PUBLIC_BASE_URL=https://你的域名`
2. 安装 Nginx 配置：`sudo -E bash scripts/deploy/install_nginx_ubuntu.sh`（需先 `export QUICKFORM_DOMAIN=...`）
3. 复制腾讯云 Nginx 证书到 `/etc/nginx/ssl/quickform/fullchain.crt` 与 `private.key`
4. `sudo nginx -t && sudo systemctl reload nginx`
5. 启动应用：`python3 app_waitress.py` 或 `deploy/systemd/quickform.service`

## 仅 HTTP（内网调试）

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`proxy_pass` 端口须与 `FLASK_PORT` 一致。

## 无 Nginx

- 可用腾讯云 **负载均衡 CLB** 挂证书，后端仍用 HTTP 连本机 5000 端口。
- 不推荐长期用 `app.run(ssl_context=...)` 直接对外 443。
