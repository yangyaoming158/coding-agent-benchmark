# 前端镜像（E10-T1）。构建上下文是仓库根目录，只 COPY frontend/。
#
# ── 为什么底座是 python:3.11-slim 而不是 node:24-alpine ──
#
# 这台机器（和多数校内网络）过代理拉 Docker Hub 镜像经常整条卡死：2026-09-21 实测
# `docker pull node:24-alpine` 两次 3 分钟都没拉下第一层。而 python:3.11-slim 是 bench-base
# 的底座，凡是建过评测镜像的机器上都已经有它；node 本身则从 npmmirror 下 tar 包（31 MB，
# 实测 12 MB/s，SHA256 和官方 SHASUMS256.txt 逐字相同）。这样前端镜像不新增任何要过
# Docker Hub 的下载，网络依赖和项目其他部分一样只有国内源。python 在这里用不到，只是搭个便车。
#
# **不改 frontend/ 下任何文件**（队友在做），所以：
#   - 没有 output: "standalone"，运行时就得带着 node_modules 跑 `next start`，镜像偏大，单机部署无所谓；
#   - NEXT_PUBLIC_API_BASE 是 Next 在**构建时**内联进页面的（frontend/src/lib/api.ts），浏览器直接
#     拿它去请求后端。所以它是 build-arg 不是运行时环境变量，改了要重建前端镜像。
#     默认 http://localhost:8000：浏览器和 compose 在同一台机器（WSL2 会把 localhost 转发给 Windows）。
#     从别的机器访问时在 .env 里改成 http://<这台机器的 IP>:8000，同时把那个前端地址加进
#     BENCH_DEV_FRONTEND_ORIGINS（后端的 CORS 放行名单）。
#
# 建：make compose-build（或 docker compose build frontend）
FROM python:3.11-slim

# ── node：开发机是 node 24 / npm 11，package-lock 是 npm 11 写的，部署用同一个大版本 ──
ARG NODE_VERSION=24.14.1
ARG NODE_DIST_MIRROR=https://npmmirror.com/mirrors/node
ARG APT_MIRROR=https://mirrors.aliyun.com/debian
# ↑ 不用清华：2026-09-21 实测它的 debian 源返回 403（deploy/backend/Dockerfile 同）
# apt 和下载都明确绕开 dockerd 注进来的代理：国内源走境外代理只会慢十倍甚至断（images/base 同款做法）
RUN sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get update \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        apt-get install -y --no-install-recommends curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(uname -m)" \
    && case "$arch" in x86_64) node_arch=x64 ;; aarch64) node_arch=arm64 ;; *) echo "不支持的架构 $arch" >&2; exit 1 ;; esac \
    && tarball="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
    && cd /tmp \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        curl -fsSL -o "$tarball" "${NODE_DIST_MIRROR}/v${NODE_VERSION}/${tarball}" \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        curl -fsSL -o SHASUMS256.txt "${NODE_DIST_MIRROR}/v${NODE_VERSION}/SHASUMS256.txt" \
    && grep " ${tarball}\$" SHASUMS256.txt | sha256sum -c - \
    && tar -xJf "$tarball" -C /usr/local --strip-components=1 --no-same-owner \
    && rm -f "$tarball" SHASUMS256.txt /usr/local/CHANGELOG.md /usr/local/LICENSE /usr/local/README.md \
    && node --version && npm --version

ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}

WORKDIR /app
# 先只拷依赖清单再 npm ci：改源码不会让依赖层失效，重建只跑 next build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry "${NPM_REGISTRY}" \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY npm ci
COPY frontend/ ./
RUN npm run build

EXPOSE 3000
CMD ["npm", "run", "start"]
