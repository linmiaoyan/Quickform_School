# Nginx 反向代理 QuickForm

Flask 已改为**仅 HTTP**（默认 `127.0.0.1:80`）。如需 HTTPS，请在网关/反向代理层自行终结 TLS。

## 1. 启动 Flask（内网服务）

在项目根目录：

```bash
python app.py
```

或设置环境变量：

- `FLASK_HOST`：监听地址，默认 `127.0.0.1`
- `FLASK_PORT`：监听端口，默认 `80`

## 2. Nginx 配置示例

将下面内容根据实际情况改好后，放入 Nginx 的 `server` 配置中（例如 `conf.d/quickform.conf` 或主配置里的 `http { ... }` 内）。

### 仅 HTTP（80 端口）

```nginx
server {
    listen 80;
    server_name your-domain.com;   # 改成你的域名或 IP

    location / {
        proxy_pass http://127.0.0.1:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

说明：

- `proxy_pass http://127.0.0.1:80` 对应 Flask 默认端口，若你改了 `FLASK_PORT`，这里要一起改。

## 3. 应用配置并重载 Nginx

- Windows（以管理员运行）：  
  `nginx -s reload`  
  或先测试配置：`nginx -t` 再 `nginx -s reload`。

- Linux：  
  `sudo nginx -t && sudo systemctl reload nginx`  
  或 `sudo nginx -s reload`。

## 4. 流程小结

1. 先启动 Flask：`python app.py`（监听 127.0.0.1:80）。
2. 再确保 Nginx 已加载上述配置并 reload。
3. 用户访问 `http://your-domain.com`，由 Nginx 转发到本机 80，Flask 无需再配 SSL。
