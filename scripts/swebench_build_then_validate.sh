#!/usr/bin/env bash
# 等本机建镜像跑完，接着把后续步骤一口气跑掉（E1-T7 路线 B，无人值守用）。
#
# 顺序：等 `cli.swebench build` 退出 → 再跑一轮 build（把中途失败的补一遍）→ import →
# validate（八步，只跑声明用例）→ review-csv（题面太短的自动 ACCEPT）→ import-review → report。
# 每一步失败不中断后面能跑的步骤，退出码和时间都记进日志，回来看日志就知道停在哪。
#
# 用法（后台跑）：
#   nohup scripts/swebench_build_then_validate.sh > var/swebench-logs/chain.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
log() { echo "[$(date '+%F %T')] $*"; }
step() { log "== $1"; shift; "$@"; log "   退出码 $?"; }

while pgrep -f 'cli.swebench build' >/dev/null; do sleep 60; done
log "第一轮 build 已退出"
# 第二轮只并行 1 个：matplotlib 的 3 个 conda 环境求解时各占 5 GB 内存，两个一起跑这台 11 GB 的机器会换页到死
# （2026-09-16 实测：两个并行 55 分钟没解完，杀掉单跑几分钟就好）。
step "build 补跑一轮（只建缺的，含 matplotlib conda 环境，串行）" make swebench-build SWEBENCH_JOBS=1
step "import" make swebench-import
step "validate" make swebench-validate
(cd backend && uv run python -m cli.swebench review-csv --out ../datasets/swebench/review-$(date +%F-%H%M)-official.csv); log "   review-csv 退出码 $?"
csv="datasets/swebench/review-$(date +%F-%H%M)-official.csv"
if [ -s "$csv" ]; then
  (cd backend && uv run python -m cli.promote import-review "../$csv" --reviewer "swebench-verified-auto"); log "   import-review 退出码 $?"
fi
step "report" make swebench-report
log "链条跑完。下一步（要人看一眼数字再做）：make dataset-stage/gate/publish DATASET=swebench-verified-subset"
