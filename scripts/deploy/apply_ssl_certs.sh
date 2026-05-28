#!/usr/bin/env bash
# 将当前目录下的腾讯云 Nginx 证书复制到标准路径
# 用法:
#   sudo bash scripts/deploy/apply_ssl_certs.sh /path/to/xxx_bundle.crt /path/to/xxx.key

set -euo pipefail

SSL_DIR="/etc/nginx/ssl/quickform"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 sudo 运行。" >&2
  exit 1
fi

if [[ $# -lt 2 ]]; then
  echo "用法: sudo $0 <证书.crt或bundle.crt> <私钥.key>" >&2
  exit 1
fi

CERT_SRC="$1"
KEY_SRC="$2"

if [[ ! -f "${CERT_SRC}" || ! -f "${KEY_SRC}" ]]; then
  echo "证书或私钥文件不存在。" >&2
  exit 1
fi

mkdir -p "${SSL_DIR}"
install -m 644 "${CERT_SRC}" "${SSL_DIR}/fullchain.crt"
install -m 600 "${KEY_SRC}" "${SSL_DIR}/private.key"

nginx -t
systemctl reload nginx
echo "证书已安装到 ${SSL_DIR}，Nginx 已重载。"
