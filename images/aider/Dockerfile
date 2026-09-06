# Aider 的 Agent 镜像（E3-T4）。
#
# 分层方式和 E2-T3 规划的一致：在环境镜像上加一层 Agent 的 CLI。
# 这里的底座暂时直接用 Golden 题的测试镜像 —— 好处是 Agent 阶段和测试阶段跑的是
# 同一个 Python，AI 在容器里看到的依赖就是判定时用的那一份。E2-T3 的构建器到位后，
# 底座换成 bench-env:<环境 id> 就行，这份 Dockerfile 的其余部分不用动。
#
# 建：make images-aider
FROM bench-golden:py311

# git 是 aider 的硬依赖（它自己要读仓库、算 diff），而 python:*-slim 里没有。
# 少了它 aider 起来就报 "GitRepo not found"，而那条报错很容易被当成工作区的问题。
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# 钉死版本。aider 的输出格式（token/cost 那一行、编辑摘要）跨版本会变，
# 而我们要从 stdout 里把 token 和 cost 解析出来 —— 不钉的话，
# 某天重建镜像，成本统计会悄悄变成空值，CI 不会有任何提示。
#
# 走国内镜像源，不走代理。aider 连依赖一共两百多兆（tree-sitter 那一串都是二进制
# 轮子），2026-09-05 实测走代理只有 47 kB/s，下到一半就 ReadTimeout 整个构建失败。
# 做成构建参数是为了换网络环境时不用改文件：
#     docker build --build-arg PIP_INDEX_URL=https://pypi.org/simple images/aider
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" \
        --retries 5 --timeout 120 \
        aider-chat==0.86.2

# 容器里跑的是宿主机的 uid（见 sandbox.container.default_container_user），
# 镜像的 /etc/passwd 里没有这个 uid，HOME 会退化成 `/`，而 `/` 不可写。
# aider 要写配置、litellm 要写缓存，写不进去就直接崩在启动阶段。
# /tmp 是 tmpfs，容器结束即消失，正合适。
ENV HOME=/tmp \
    XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache

# aider 会调 git。工作区里那个仓库是 harness 建的，没有配 user.name/user.email，
# 而 git 在需要写提交对象时会因此直接失败。我们用 --no-auto-commits 让它别提交，
# 但它内部仍有些路径会碰到这个检查，所以用环境变量兜一层 ——
# 用环境变量而不是 git config，是为了不往工作区的 .git/config 里写东西
# （那会变成补丁里的一处改动）。
ENV GIT_AUTHOR_NAME=bench \
    GIT_AUTHOR_EMAIL=bench@localhost \
    GIT_COMMITTER_NAME=bench \
    GIT_COMMITTER_EMAIL=bench@localhost

WORKDIR /workspace
