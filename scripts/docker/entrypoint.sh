#!/bin/sh
# 容器入口：以 root 启动时，先把数据目录交给 stockarena 用户（挂载的宿主机目录常常属于 root），再降权运行。
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data
  if [ "$(stat -c %u /app/data)" != "$(id -u stockarena)" ]; then
    chown -R stockarena:stockarena /app/data
  fi
  exec setpriv --reuid=stockarena --regid=stockarena --init-groups "$@"
fi
exec "$@"
