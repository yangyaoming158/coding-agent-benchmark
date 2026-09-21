#!/usr/bin/env bash
# API / Worker / cli 容器的入口：先把"以谁的身份跑"定下来，再交给真正的命令。
#
# 为什么不在 Dockerfile 里写死 USER：
#   宿主机用户的 uid 事先不知道。工作区（var/workspaces）、制品（var/artifacts）都写在
#   挂进来的宿主机目录上，容器里的 uid 和宿主机不一致，宿主机上就得 sudo 才能删。
#   更要紧的是评测容器：sandbox.container.default_container_user() 让评测容器跟着 harness
#   的 uid 跑；harness 是 root 时退到 nobody，而工作区是 harness 建的 —— root 建的目录
#   nobody 写不进去，被测 AI 改不了一个文件，全部判 UNRESOLVED，而且看起来像 AI 自己不行。
#
# 做法：读仓库目录的属主，切成那个 uid/gid（BENCH_UID / BENCH_GID 可以显式覆盖）。
# 起评测容器要用 docker.sock，所以再把 socket 的属组挂到这个用户名下。
set -euo pipefail

: "${BENCH_REPO_DIR:?compose 没把 BENCH_REPO_DIR 传进来，这个镜像只能通过 docker-compose.yml 起}"

uid="${BENCH_UID:-$(stat -c %u "$BENCH_REPO_DIR")}"
gid="${BENCH_GID:-$(stat -c %g "$BENCH_REPO_DIR")}"

if [[ "$uid" == "0" ]]; then
  # 仓库属主就是 root（或者显式要 root）。这和在宿主机上以 root 跑 Worker 是同一种情况：
  # 评测容器会退到 nobody，工作区要事先 chown —— 不在这里替用户做决定，原样放行。
  exec "$@"
fi

# 建组和用户（幂等：已经有这个 gid / uid 就复用现成的名字）
group_name=$(getent group "$gid" | cut -d: -f1 || true)
if [[ -z "$group_name" ]]; then
  groupadd -g "$gid" bench
  group_name=bench
fi
user_name=$(getent passwd "$uid" | cut -d: -f1 || true)
if [[ -z "$user_name" ]]; then
  useradd -u "$uid" -g "$gid" -d /home/bench -M -s /bin/bash bench
  user_name=bench
fi
mkdir -p /home/bench && chown "$uid:$gid" /home/bench
export HOME=/home/bench

# docker.sock 的属组：宿主机上一般是 docker 组（Ubuntu 默认 660 root:docker）。
# 没挂 socket 的服务（api、migrate）跳过这一段。
sock=/var/run/docker.sock
if [[ -S "$sock" ]]; then
  sock_gid=$(stat -c %g "$sock")
  if [[ "$sock_gid" != "0" ]]; then
    sock_group=$(getent group "$sock_gid" | cut -d: -f1 || true)
    if [[ -z "$sock_group" ]]; then
      groupadd -g "$sock_gid" dockersock
      sock_group=dockersock
    fi
    usermod -aG "$sock_group" "$user_name"
  fi
fi

# --init-groups 会把上面加的附属组一起带上；setpriv 在 util-linux 里，slim 镜像自带
exec setpriv --reuid="$uid" --regid="$gid" --init-groups -- "$@"
