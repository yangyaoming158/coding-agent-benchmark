# 平台自己的运行时镜像（E10-T1）：API、Worker、migrate、cli 四个服务共用。
#
# 里面**只有 Python 依赖，没有代码**。代码从宿主机检出按原路径挂进容器 ——
# Worker 用宿主机的 dockerd 起评测容器，交给 dockerd 的路径必须和宿主机一模一样，
# 理由和取舍写在 docker-compose.yml 顶部。所以改代码不用重建镜像，改了 pyproject / uv.lock 才要。
#
# 建：make compose-build（或 docker compose build）
FROM python:3.11-slim
# ↑ 项目声明的目标版本（pyproject 的 target-version = py311，评测镜像 bench-base 也是 3.11-slim）。
#   开发机的 .venv 是 3.12.3，uv.lock 对两个版本都成立；选 3.11 还有个实际原因：它已经在每台建过
#   bench-base 的机器上，不用再过一次代理拉基础镜像（2026-09-21 实测拉 3.12-slim 十分钟只下了 6 MB）。

# ── 系统包：git 是硬依赖 ──────────────────────────────────────
#
# 工作区物化靠 `git archive`（app/sandbox/workspace.py），而且要 ≥ 2.32 才认
# GIT_CONFIG_GLOBAL（scripts/check_env.py 的门槛）。Debian 12/13 的 git 都够。
#
# apt 走国内镜像源、明确绕开代理 —— 和 images/base/Dockerfile 同一套做法：
# dockerd 会把自己的代理配置注进每个构建步骤，让国内镜像源走境外代理只会慢十倍。
# 默认阿里云不是清华：2026-09-21 实测清华的 debian 和 pypi 两个源都返回 403（images/base 用的那个），
# USTC 的 pypi 对 uv 的并发下载限流（429）。阿里云两样都正常。换源传 build-arg，不用改文件。
ARG APT_MIRROR=https://mirrors.aliyun.com/debian
RUN sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        apt-get update \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Python 依赖：按 uv.lock 装进 /opt/venv ───────────────────
#
# 版本以 backend/uv.lock 为准，不在仓库里另放一份 requirements.txt 让两处迟早对不上：
# 构建时 `uv export` 从锁里现导一份（只有 名字==版本 和 sha256，没有下载地址），再从镜像源装。
# 不直接 `uv sync --frozen`：它按锁里记的**绝对 URL** 下载（清华源），`--default-index` 换不掉，
# 2026-09-21 就是这么栽的（清华 403）。导出再装，源就只由下面这个 ARG 决定；哈希照样校验。
# `--no-emit-project` 是因为代码不在镜像里（见顶部）。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_CONCURRENT_DOWNLOADS=4 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv
WORKDIR /build
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --output-file requirements.txt \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --index-url "${PIP_INDEX_URL}" \
        --require-hashes -r requirements.txt \
    && rm -rf /build
ENV PATH=/opt/venv/bin:${PATH}

# ── 入口和健康检查 ───────────────────────────────────────────
#
# 入口脚本负责"以谁的身份跑"：容器默认是 root，但工作区、制品都写在挂进来的宿主机目录上，
# root 写出来的文件宿主机用户动不了；而且 harness 是 root 时评测容器会退到 nobody，
# 工作区又是 root 建的，评测容器写不进去。脚本把身份切成仓库目录的属主，行为就和在
# 宿主机上直接 `make worker` 一模一样。细节见 entrypoint.sh。
COPY deploy/backend/entrypoint.sh /usr/local/bin/bench-entrypoint
COPY deploy/backend/healthcheck.py /usr/local/bin/bench-healthcheck
RUN chmod 0755 /usr/local/bin/bench-entrypoint /usr/local/bin/bench-healthcheck
ENTRYPOINT ["bench-entrypoint"]
# 真正的命令由 docker-compose.yml 按服务给：uvicorn / python -m app.worker / alembic
