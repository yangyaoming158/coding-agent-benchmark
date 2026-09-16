#!/usr/bin/env bash
# 反复跑 `make swebench-fetch-blobs` 直到抽中题的镜像层全部下到 D 盘，然后 `make swebench-load`（E1-T7）。
#
# 为什么走 Windows：同一个 VPN 代理，Windows 侧 14 MB/s，WSL 侧不到 1 MB/s（2026-09-16 实测），
# 瓶颈在 WSL 到宿主那一跳。`curl.exe` 是 Windows 自带的 curl，WSL 里能直接调。
# 线路仍会抖，单个层下坏了会删掉重来，所以外面套个循环。
#
# 用法：
#   nohup scripts/swebench_fetch_loop.sh > var/swebench-logs/fetch-loop.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
round=0
until make swebench-fetch-blobs; do
  round=$((round + 1))
  echo "[$(date '+%F %T')] 第 ${round} 轮没下完，120 秒后再来"
  sleep 120
done
echo "[$(date '+%F %T')] 层全部下到，开始装载"
make swebench-load
echo "[$(date '+%F %T')] 完成。下一步：make swebench-import && make swebench-validate"
