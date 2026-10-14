#!/usr/bin/env bash
# 部署 / 更新到自己的 Linux 服务器（目标目录 ~/stockarena，需要能免密 ssh 登录）。
# 用法：在仓库根目录执行  bash scripts/deploy.sh            部署当前提交
#                         bash scripts/deploy.sh --rollback 回到上一次部署的版本（见下方说明）
# 服务器地址通过环境变量 SA_DEPLOY_HOST 指定（可写 user@host 或 ~/.ssh/config 里的别名），
# 也可以写在 scripts/deploy.env 里（不进版本库），例如：SA_DEPLOY_HOST=user@10.0.0.2
# 服务器上需要先安装 uv 和 systemd 用户服务（模板见 scripts/stockarena.service），详见 docs/DEPLOY.md。
#
# 步骤：检查工作区已提交 → 本地打包前端 → 上传到服务器的 .release 目录并检查完整
#       → 停止服务并备份数据库（保留最近 10 份）→ 整体替换 backend、config、docs、scripts、frontend/dist
#       → 在独立环境（backend/.venv，uv 管理的 Python 3.13）中同步依赖 → 启动服务并检查能否访问。
# 替换是整目录替换，服务器上的代码与仓库完全一致（仓库里删掉的文件不会残留）。保留不动的只有：
#   data/（数据库、日志、主密钥）、config/config.yaml（及 config.yaml.* 备份）、backend/.venv。
# 上一版放在 .previous，--rollback 会换回去。数据库迁移在服务启动时自动执行：如果新版本带了数据库迁移，
# 回滚代码后旧版本可能无法启动，这时还要从 data/backup/ 恢复部署前的数据库。
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

# 由部署管理、整体替换的路径
MANAGED="backend config docs scripts frontend/dist README.md LICENSE NOTICE DISCLAIMER.md"

start_and_check() {
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
}

if [ "${1:-}" = "--rollback" ]; then
  echo "== 回滚：把 .previous 中的上一版换回来（当前版本移到 .previous，再执行一次 --rollback 可以换回）"
  ssh "$HOST" "set -e
    cd ~/$DIR
    test -d .previous/backend || { echo '没有可回滚的上一版（.previous 不存在）' >&2; exit 1; }
    systemctl --user stop stockarena || true
    mv backend/.venv .venv.keep
    for p in $MANAGED; do
      if [ -e .previous/\$p ]; then
        mkdir -p .swap/\$(dirname \$p)
        if [ -e \$p ]; then mv \$p .swap/\$p; fi
        mv .previous/\$p \$p
        if [ -e .swap/\$p ]; then mkdir -p .previous/\$(dirname \$p); mv .swap/\$p .previous/\$p; fi
      fi
    done
    rm -rf .swap
    mv .venv.keep backend/.venv"
  start_and_check
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区有未提交的改动，请先提交后再部署" >&2
  exit 1
fi
echo "部署版本：$(git log --oneline -1)"

echo "== 打包前端"
(cd frontend && npm run build >/dev/null)

echo "== 上传到服务器的 .release 目录"
ssh "$HOST" "rm -rf ~/$DIR/.release && mkdir -p ~/$DIR/.release/frontend/dist"
git archive --format=tar HEAD backend config docs scripts README.md LICENSE NOTICE DISCLAIMER.md \
  | ssh "$HOST" "tar -x -C ~/$DIR/.release"
tar -C frontend/dist -cf - . | ssh "$HOST" "tar -x -C ~/$DIR/.release/frontend/dist"
ssh "$HOST" "test -f ~/$DIR/.release/backend/pyproject.toml && test -f ~/$DIR/.release/frontend/dist/index.html" \
  || { echo "上传不完整，没有替换任何文件" >&2; exit 1; }

echo "== 停止服务并备份数据库"
ssh "$HOST" "set -e
  systemctl --user stop stockarena || true
  mkdir -p ~/$DIR/data/backup
  if [ -f ~/$DIR/data/stockarena.db ]; then
    cp ~/$DIR/data/stockarena.db ~/$DIR/data/backup/stockarena-\$(date +%Y%m%d-%H%M%S).db
    ls -1t ~/$DIR/data/backup/stockarena-*.db | tail -n +11 | xargs -r rm --
  fi"

echo "== 替换代码（保留 data/、config/config.yaml、backend/.venv；上一版留在 .previous）"
ssh "$HOST" "set -e
  cd ~/$DIR
  if [ -d backend/.venv ]; then mv backend/.venv .release/backend/.venv; fi
  for f in config/config.yaml config/config.yaml.*; do
    if [ -e \"\$f\" ]; then cp -p \"\$f\" .release/config/; fi
  done
  rm -rf .previous
  for p in $MANAGED; do
    if [ -e \$p ]; then mkdir -p .previous/\$(dirname \$p); mv \$p .previous/\$p; fi
    mkdir -p \$(dirname \$p)
    mv .release/\$p \$p
  done
  rm -rf .release"

start_and_check
