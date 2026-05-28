#!/usr/bin/env bash
# QuickForm 校园版 — Ubuntu/Debian 安装 Nginx 并写入站点配置
# 用法（项目根目录）:
#   export QUICKFORM_DOMAIN=example.com
#   export QUICKFORM_APP_ROOT=/opt/quickform
#   sudo -E bash scripts/deploy/install_nginx_ubuntu.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOMAIN="${QUICKFORM_DOMAIN:-}"
APP_ROOT="${QUICKFORM_APP_ROOT:-/opt/quickform}"
UPSTREAM_PORT="${QUICKFORM_UPSTREAM_PORT:-5000}"
SSL_DIR="/etc/nginx/ssl/quickform"
SSL_CERT="${SSL_CERT_PATH:-${SSL_DIR}/fullchain.crt}"
SSL_KEY="${SSL_KEY_PATH:-${SSL_DIR}/private.key}"
SITE_AVAILABLE="/etc/nginx/sites-available/quickform.conf"
SITE_ENABLED="/etc/nginx/sites-enabled/quickform.conf"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 或 sudo 运行此脚本。" >&2
  exit 1
fi

if [[ -z "${DOMAIN}" ]]; then
  echo "请设置环境变量 QUICKFORM_DOMAIN（例如 export QUICKFORM_DOMAIN=quickform.example.com）" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx

mkdir -p "${SSL_DIR}"
chmod 700 "${SSL_DIR}"

if [[ ! -f "${SSL_CERT}" || ! -f "${SSL_KEY}" ]]; then
  echo "提示：尚未检测到证书文件。"
  echo "  请将腾讯云 Nginx 证书复制到："
  echo "    ${SSL_CERT}"
  echo "    ${SSL_KEY}"
  echo "  复制完成后执行: nginx -t && systemctl reload nginx"
  # 生成自签名占位仅用于 nginx -t 通过（可选）；若证书已存在则跳过
  if [[ ! -f "${SSL_CERT}" ]]; then
    openssl req -x509 -nodes -days 1 -newkey rsa:2048 \
      -keyout "${SSL_KEY}" -out "${SSL_CERT}" \
      -subj "/CN=${DOMAIN}" 2>/dev/null || true
    echo "已生成 1 天自签名临时证书，便于先完成 Nginx 配置；上线前务必替换为腾讯云证书。"
  fi
fi

TEMPLATE="${ROOT_DIR}/deploy/nginx/quickform.conf.template"
if [[ ! -f "${TEMPLATE}" ]]; then
  echo "找不到模板: ${TEMPLATE}" >&2
  exit 1
fi

sed \
  -e "s|__SERVER_NAME__|${DOMAIN}|g" \
  -e "s|__SSL_CERT_PATH__|${SSL_CERT}|g" \
  -e "s|__SSL_KEY_PATH__|${SSL_KEY}|g" \
  -e "s|__APP_ROOT__|${APP_ROOT}|g" \
  -e "s|__UPSTREAM_PORT__|${UPSTREAM_PORT}|g" \
  "${TEMPLATE}" > "${SITE_AVAILABLE}"

ln -sf "${SITE_AVAILABLE}" "${SITE_ENABLED}"

# 禁用默认站点，避免与 80/443 冲突
if [[ -f /etc/nginx/sites-enabled/default ]]; then
  rm -f /etc/nginx/sites-enabled/default
fi

nginx -t
systemctl enable nginx
systemctl reload nginx

echo ""
echo "Nginx 已配置完成。"
echo "  域名: ${DOMAIN}"
echo "  上游: 127.0.0.1:${UPSTREAM_PORT} （请用 app_waitress.py + .env 启动应用）"
echo "  证书: ${SSL_CERT}"
echo "  私钥: ${SSL_KEY}"
echo ""
echo "下一步："
echo "  1) 将腾讯云证书覆盖到上述路径（若当前为临时自签名）"
echo "  2) 配置 ${APP_ROOT}/.env 中 PUBLIC_BASE_URL=https://${DOMAIN}"
echo "  3) systemctl enable --now quickform  （见 deploy/systemd/quickform.service）"
