#!/usr/bin/env bash
# 反复跑 `make swebench-pull` 直到抽中的官方镜像全部拉到，然后备好 git 镜像（E1-T7）。
#
# 为什么要一个循环：这台机器过代理拉 Docker Hub 会整条连接卡死（2026-09-15 实测），
# `cli.swebench pull` 一轮里连着几个失败就会先停（--give-up-after），拉完的层 docker 留着，
# 睡一会再来接着拉。全部拉到时 `make swebench-pull` 退出码为 0，循环才结束。
#
# 每轮之间先 `make swebench-warm`：镜像站（docker.1ms.run）是"先有人要才去 Docker Hub 缓存"的，
# 一个层第一次被要时只给几 KB/s，等它自己抓完再要才快；预热就是把每一层都碰一下让它去抓。
#
# 用法（后台跑，日志落 var/swebench-logs/）：
#   mkdir -p var/swebench-logs
#   nohup scripts/swebench_pull_loop.sh > var/swebench-logs/pull-loop.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
round=0
until make swebench-pull; do
  round=$((round + 1))
  # 镜像站是"先有人要才去 Docker Hub 缓存"的：每层碰几秒让它先去抓，等它抓完 docker pull 才快
  echo "[$(date '+%F %T')] 第 ${round} 轮没拉完，先预热镜像站，再等 300 秒"
  make swebench-warm || true
  sleep 300
done
echo "[$(date '+%F %T')] 镜像全部拉到，接着备 git 镜像"
until make swebench-mirror; do
  echo "[$(date '+%F %T')] git 镜像没备齐，300 秒后再来"
  sleep 300
done
echo "[$(date '+%F %T')] 全部就绪。下一步：make swebench-import && make swebench-validate"
