#!/usr/bin/env bash
# 本地开发用的 Postgres 容器。
#
# 端口用 5433 不用 5432，避免和这台机器上别的项目抢端口。
#
#   ./scripts/dev_db.sh up      起容器（已存在就直接启动）
#   ./scripts/dev_db.sh down    停容器，数据保留
#   ./scripts/dev_db.sh reset   删掉容器和数据，重新起一个空库
#   ./scripts/dev_db.sh psql    连进去看
set -euo pipefail

CONTAINER=bench-postgres
IMAGE=postgres:16-alpine
PORT=5433
DB=bench
USER=bench
PASSWORD=bench

start_container() {
  if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    docker start "$CONTAINER" >/dev/null
  else
    docker run -d --name "$CONTAINER" \
      -e POSTGRES_DB="$DB" -e POSTGRES_USER="$USER" -e POSTGRES_PASSWORD="$PASSWORD" \
      -p "$PORT:5432" "$IMAGE" >/dev/null
  fi
  # 等到真的能连上为止。容器起来不等于数据库能用，中间还有初始化。
  for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" pg_isready -U "$USER" -d "$DB" >/dev/null 2>&1; then
      echo "Postgres 就绪：postgresql+psycopg://$USER:$PASSWORD@localhost:$PORT/$DB"
      return 0
    fi
    sleep 1
  done
  echo "等了 60 秒 Postgres 还没起来，看看 docker logs $CONTAINER" >&2
  return 1
}

case "${1:-up}" in
  up)    start_container ;;
  down)  docker stop "$CONTAINER" >/dev/null && echo "已停止 $CONTAINER（数据保留）" ;;
  reset) docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; start_container ;;
  psql)  docker exec -it "$CONTAINER" psql -U "$USER" -d "$DB" ;;
  *)     echo "用法：$0 {up|down|reset|psql}" >&2; exit 1 ;;
esac
