#!/usr/bin/env bash
# compose 部署冒烟（E10-T1 AC ③）：在 compose 起的平台上，用 Golden 4 题跑 Oracle 和 Noop 两个哨兵。
#
# 期望 Oracle 4/4 RESOLVED、Noop 0/4。这一趟走完，等于证明了 Worker 在容器里真能通过
# docker.sock 起评测容器、工作区路径没走样、判定链路完整 —— 不只是"进程起来了"。
#
#   make compose-smoke                 # 平台要先 make compose-up
#   ALLOW_DIRTY=1 make compose-smoke   # 工作区有未提交改动时：协议 C-27 会拒绝建实验，
#                                      # 加这个放行，结果标 dirty=true，只能当冒烟不能当成绩
#
# 幂等：git 镜像、哨兵 Agent、题目入库、快照都是"有了就跳过"，重复跑只会多两个实验。
set -euo pipefail
cd "$(dirname "$0")/.."
export BENCH_REPO_DIR="${BENCH_REPO_DIR:-$PWD}"

GOLDEN_IMAGE=bench-golden:py311
GOLDEN_TASKS=(bench-golden__auth-2 bench-golden__cart-3 bench-golden__pager-4 bench-golden__textkit-1)
# 单题几十秒，4 题 × 2 个实验串行也就几分钟；20 分钟还没完就是卡住了
WAIT_LIMIT_S=1200

cli() { docker compose run --rm -T cli "$@"; }
step() { printf '\n── %s\n' "$*"; }

step "1/6 测试镜像 ${GOLDEN_IMAGE}（Golden 题的测试阶段在它里面跑 pytest）"
if docker image inspect "$GOLDEN_IMAGE" >/dev/null 2>&1; then
  echo "已存在，跳过"
else
  docker build -t "$GOLDEN_IMAGE" images/golden
fi

step "2/6 Golden 题：生成 git 镜像 → 写入哨兵 Agent → 题目入库"
cli python -m cli.golden build
cli python -m cli.seed
cli python -m cli.queue seed-golden

step "3/6 八步验证这 4 道题（起容器，约 1 分钟）—— 只有 VALID 的题能进数据集"
task_args=()
for t in "${GOLDEN_TASKS[@]}"; do task_args+=(--task "$t"); done
cli python -m cli.validate run "${task_args[@]}"

step "4/6 冻快照 golden（已有且内容一样时是 noop）"
cli python -m cli.dataset stage --dataset-id golden-v1 --slug golden

step "5/6 建两个实验：Oracle（官方补丁，应 4/4）和 Noop（空补丁，应 0/4）"
dirty_flag=()
if [[ -n "${ALLOW_DIRTY:-}" ]]; then dirty_flag=(--allow-dirty); fi
enqueue() {
  # 输出形如"实验 #12（compose-smoke-oracle）已建，投了 4 条 EVAL_TASK 作业"，把 12 抠出来
  local out
  out=$(cli python -m cli.queue enqueue --agent "$1" --set golden --name "compose-smoke-$1" "${dirty_flag[@]}")
  echo "$out" >&2
  sed -n 's/^实验 #\([0-9][0-9]*\).*/\1/p' <<<"$out" | head -1
}
oracle_run=$(enqueue oracle)
noop_run=$(enqueue noop)
if [[ -z "$oracle_run" || -z "$noop_run" ]]; then
  echo "建实验失败（上面有原因；工作区不干净就加 ALLOW_DIRTY=1）" >&2
  exit 1
fi

step "6/6 等 Worker 跑完（实验 #${oracle_run} / #${noop_run}）"
api_port=$(docker compose port api 8000 | sed 's/.*://')
summary() {
  curl -sf "http://localhost:${api_port}/api/runs/$1" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d["status"], d["completed_tasks"], d["total_tasks"], d["resolved_count"], d["infra_failure_count"], str(d["dirty"]).lower())'
}
wait_run() {
  local id=$1 waited=0 line
  while true; do
    line=$(summary "$id" || echo "UNREACHABLE 0 0 0 0 false")
    read -r status done total resolved infra dirty <<<"$line"
    case "$status" in
      COMPLETED|PARTIAL|FAILED|CANCELLED) echo "$line"; return 0 ;;
    esac
    if (( waited >= WAIT_LIMIT_S )); then
      echo "实验 #$id 等了 ${WAIT_LIMIT_S}s 还是 $status（$done/$total），看日志：make compose-logs SERVICE=worker" >&2
      return 1
    fi
    # 进度打到 stderr：这个函数的 stdout 被 $(...) 捕获，混进去会把结果行搅乱
    printf '\r  #%s %s %s/%s  ' "$id" "$status" "$done" "$total" >&2
    sleep 5; waited=$((waited + 5))
  done
}
oracle_line=$(wait_run "$oracle_run")
noop_line=$(wait_run "$noop_run")
printf '\r' >&2
read -r o_status o_done o_total o_resolved o_infra o_dirty <<<"$oracle_line"
read -r n_status n_done n_total n_resolved n_infra n_dirty <<<"$noop_line"

echo
echo "Oracle #${oracle_run}: ${o_status}  解决 ${o_resolved}/${o_total}  平台故障 ${o_infra}  dirty=${o_dirty}"
echo "Noop   #${noop_run}: ${n_status}  解决 ${n_resolved}/${n_total}  平台故障 ${n_infra}  dirty=${n_dirty}"
if [[ "$o_resolved" == "$o_total" && "$o_total" == "4" && "$n_resolved" == "0" && "$n_total" == "4" && "$o_infra" == "0" && "$n_infra" == "0" ]]; then
  echo "✅ 冒烟通过：Worker 在容器里能起评测容器，判定链路完整"
  echo "细账：make compose-cli CMD=\"python -m cli.experiment status --run ${oracle_run}\""
else
  echo "❌ 冒烟没过。看 Worker 日志：make compose-logs SERVICE=worker" >&2
  exit 1
fi
