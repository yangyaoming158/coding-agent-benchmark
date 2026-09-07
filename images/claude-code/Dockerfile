# Claude Code 的 Agent 镜像（E3-T5）。
#
# 分层方式和 Aider 那份一致：在环境镜像上加一层 Agent 的 CLI。底座暂时直接用
# Golden 题的测试镜像 —— 好处是 Agent 阶段和测试阶段跑的是同一个 Python，
# AI 在容器里看到的依赖就是判定时用的那一份。E2-T3 的构建器到位后，
# 底座换成 bench-env:<环境 id> 即可，这份 Dockerfile 的其余部分不用动。
#
# 建：make images-claude-code
FROM bench-golden:py311

# git 是被测 AI 的常用工具（读历史、看 diff），另外三个是装 Node 用的。
#
# apt 走国内镜像源，而且**明确绕开代理**。2026-09-06 实测：dockerd 会把
# ~/.docker/config.json 里那个代理注进构建步骤，走它拉 deb.debian.org 是
# 9.2 秒一个请求（直连 1.4 秒），而 nodejs 连依赖两百多兆 —— 第一次构建挂了
# 14 分钟，一个包都没下完。清华源直连是 417 kB/s。
#
# 换网络环境时不用改文件：
#     docker build --build-arg APT_MIRROR=http://deb.debian.org/debian images/claude-code
ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
RUN sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        apt-get update \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        apt-get install -y --no-install-recommends git ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Node 直接装官方二进制，不用 Debian 仓库里那个。
#
# 2026-09-06 实测踩到的：trixie 的 nodejs 是 20.19.2，而
# claude-code 2.1.236 的 package.json 写着 `node: >=22.0.0`。
# npm 只打一条 EBADENGINE 警告就照装不误 —— **装完了，跑起来才崩**，
# 而且崩在容器里，表现成一次莫名其妙的 Agent 运行时错误。
#
# 顺带解决 npm 版本：tarball 里自带配套的 npm，比仓库里那个 9.2.0 新。
ARG NODE_VERSION=v22.16.0
ARG NODE_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/nodejs-release
RUN env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        curl -fsSL "${NODE_MIRROR}/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz" \
        -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
        --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

# 钉死版本，而且钉的是 npm 上的 `stable` 标签、不是 `latest`。
# 这个包发得极勤（2026-09-06 查到 507 个版本，latest 2.1.263 / stable 2.1.236），
# 而 stream-json 的事件字段是我们解析 token 和轨迹的依据 —— 不钉的话，
# 某天重建镜像，成本统计会悄悄变成空值，CI 不会有任何提示。
#
# npm 走国内镜像源。Aider 那次实测走代理只有 47 kB/s、下到一半 ReadTimeout，
# 这个包连依赖也是几十兆。做成构建参数是为了换网络环境时不用改文件：
#     docker build --build-arg NPM_REGISTRY=https://registry.npmjs.org images/claude-code
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG CLAUDE_CODE_VERSION=2.1.236
RUN env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        npm install -g --registry "$NPM_REGISTRY" \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# 容器里跑的是宿主机的 uid（见 sandbox.container.default_container_user），
# 镜像的 /etc/passwd 里没有这个 uid，HOME 会退化成 `/`，而 `/` 不可写。
# claude 要写配置和会话记录，写不进去就直接崩在启动阶段。
# /tmp 是 tmpfs，容器结束即消失，正合适 —— 而且它**不在工作区里**，
# 不会变成 git diff 里凭空多出来的改动。
ENV HOME=/tmp \
    XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache \
    CLAUDE_CONFIG_DIR=/tmp/.claude

# 关掉三样会花钱或会联外的东西：
# - 自动更新：钉死版本的意义就在于它别自己升级
# - 遥测 / 错误上报：评测容器里跑的是别人的仓库代码，不该往外发
# - 非必要模型调用（会话摘要之类）：那些 token 也会记在这次评测的账上，
#   而它们和"修这道题"无关，会让成本统计凭空多一截
ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    DISABLE_NON_ESSENTIAL_MODEL_CALLS=1 \
    DISABLE_BUG_COMMAND=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# claude 会调 git。工作区里那个仓库是 harness 建的，没有配 user.name/user.email，
# 而 git 在需要写提交对象时会因此直接失败。用环境变量而不是 git config，
# 是为了不往工作区的 .git/config 里写东西（那会变成补丁里的一处改动）。
ENV GIT_AUTHOR_NAME=bench \
    GIT_AUTHOR_EMAIL=bench@localhost \
    GIT_COMMITTER_NAME=bench \
    GIT_COMMITTER_EMAIL=bench@localhost

WORKDIR /workspace
