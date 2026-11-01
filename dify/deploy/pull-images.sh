#!/usr/bin/env bash
# 串行拉取 Dify 镜像。
#
# 为什么不用 `docker compose pull`：实测并发 5 路时总吞吐崩到 ~0.1MB/s
# （6 分钟只进 36MB），单路串行能稳定到 ~1MB/s。这条线路上串行反而快 10 倍。
#
# 为什么按「小 → 大」排序：先让 postgres/redis/nginx 这类小依赖就位，
# 万一中途要中断，也能先验证 compose 的骨架是否正常。
set -u

# 关键：docker-credential-desktop 在 resources/bin 下，脚本用绝对路径调 docker 时
# 若 PATH 里没有这个目录，拉取会直接报 "executable file not found in %PATH%"。
export PATH="/c/Program Files/Docker/Docker/resources/bin:$PATH"

DOCKER="/c/Program Files/Docker/Docker/resources/bin/docker.exe"
M1=docker.1ms.run

IMAGES=(
  "$M1/library/busybox:latest"
  "$M1/library/nginx:latest"
  "$M1/library/postgres:15-alpine"
  "$M1/library/redis:6-alpine"
  "$M1/ubuntu/squid:latest"
  "$M1/langgenius/dify-sandbox:0.2.15"
  "$M1/langgenius/dify-web:1.18.0"
  "$M1/langgenius/dify-plugin-daemon:0.6.11-local"
  "$M1/langgenius/dify-agent-local-sandbox:1.18.0"
  "$M1/langgenius/dify-agent-backend:1.18.0"
  "$M1/langgenius/dify-api:1.18.0"
)

TOTAL=${#IMAGES[@]}
i=0
for img in "${IMAGES[@]}"; do
  i=$((i + 1))
  echo "===== [$i/$TOTAL] $(date +%H:%M:%S) $img"
  if "$DOCKER" image inspect "$img" >/dev/null 2>&1; then
    echo "     已存在，跳过"
    continue
  fi
  start=$(date +%s)
  if "$DOCKER" pull "$img" >/tmp/pull-$$.log 2>&1; then
    echo "     OK  $(( $(date +%s) - start ))s  $(grep -c 'Pull complete' /tmp/pull-$$.log) layers"
  else
    echo "     FAIL  $(( $(date +%s) - start ))s"
    tail -3 /tmp/pull-$$.log | sed 's/^/       /'
  fi
done

echo "===== ALL DONE $(date +%H:%M:%S)"
"$DOCKER" images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep -E "dify|postgres|redis|nginx|squid" | sort
