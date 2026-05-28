此目录仅作说明，不要把真实证书提交到 Git。

部署到服务器后，请将腾讯云「Nginx」格式下载的文件放到：

  /etc/nginx/ssl/quickform/fullchain.crt   ← 证书（*_bundle.crt 或 .crt）
  /etc/nginx/ssl/quickform/private.key       ← 私钥（.key）

文件名可与模板不一致，但需与 install 脚本或 quickform.conf 中的路径一致。
