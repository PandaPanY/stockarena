#!/usr/bin/env bash
# 部署 / 更新到自己的 Linux 服务器（目标目录 ~/stockarena，需要能免密 ssh 登录）。
# 用法：在仓库根目录执行  bash scripts/deploy.sh
# 服务器地址通过环境变量 SA_DEPLOY_HOST 指定（可写 user@host 或 ~/.ssh/config 里的别名），
# 也可以写在 scripts/deploy.env 里（不进版本库），例如：SA_DEPLOY_HOST=user@10.0.0.2
# 服务器上需要先安装 uv 和 systemd 用户服务（模板见 scripts/stockarena.service），详见 README「服务器部署」。
#
# 步骤：检查工作区已提交 → 本地打包前端 → 停止服务并备份数据库（保留最近 10 份）→ 上传代码与前端页面
#       → 在服务器的独立环境（backend/.venv，uv 管理的 Python 3.13）中同步依赖 → 启动服务并检查能否访问。
# 不会覆盖服务器上的 config/config.yaml 与 data/（数据库、日志）。数据库迁移在服务启动时自动执行。
set -euo pipefail

cd "$(dirname "$0")/.."
if [ -f scripts/deploy.env ]; then
  # shellcheck disable=SC1091
  . scripts/deploy.env
fi
HOST=${SA_DEPLOY_HOST:?请设置 SA_DEPLOY_HOST（服务器地址），或写在 scripts/deploy.env 里}
DIR=${SA_DEPLOY_DIR:-stockarena}
PORT=${SA_DEPLOY_PORT:-18080}
# 部署后用于检查服务的地址（默认取 SA_DEPLOY_HOST 去掉 user@ 部分）
URL_HOST=${SA_DEPLOY_URL_HOST:-${HOST#*@}}

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区有未提交的改动，请先提交后再部署" >&2
  exit 1
fi
echo "部署版本：$(git log --oneline -1)"

echo "== 打包前端"
(cd frontend && npm run build >/dev/null)

echo "== 停止服务并备份数据库"
ssh "$HOST" "set -e
  systemctl --user stop stockarena || true
  mkdir -p ~/$DIR/data/backup
  if [ -f ~/$DIR/data/stockarena.db ]; then
    cp ~/$DIR/data/stockarena.db ~/$DIR/data/backup/stockarena-\$(date +%Y%m%d-%H%M%S).db
    ls -1t ~/$DIR/data/backup/stockarena-*.db | tail -n +11 | xargs -r rm --
  fi"

echo "== 上传代码与前端页面"
git archive --format=tar HEAD backend config docs scripts README.md | ssh "$HOST" "mkdir -p ~/$DIR && tar -x -C ~/$DIR"
ssh "$HOST" "rm -rf ~/$DIR/frontend/dist.new && mkdir -p ~/$DIR/frontend/dist.new"
tar -C frontend/dist -cf - . | ssh "$HOST" "set -e
  tar -x -C ~/$DIR/frontend/dist.new
  rm -rf ~/$DIR/frontend/dist
  mv ~/$DIR/frontend/dist.new ~/$DIR/frontend/dist"

echo "== 同步依赖并启动服务"
ssh "$HOST" "set -e
  export PATH=\$HOME/.local/bin:\$PATH
  cd ~/$DIR/backend
  uv sync --frozen --no-dev --python 3.13 --python-preference only-managed -q
  systemctl --user start stockarena
  sleep 10
  systemctl --user is-active stockarena"

curl -s --noproxy '*' --max-time 20 "http://$URL_HOST:$PORT/api/auth/status"
echo
echo "完成：http://$URL_HOST:$PORT"
