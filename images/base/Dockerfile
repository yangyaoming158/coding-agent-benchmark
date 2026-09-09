# bench-base —— 三层镜像的第一层（E2-T3，`05-sandbox.md` §10.4）。
#
# 这一层放"所有环境都要有、而且和具体仓库无关"的东西：编译工具链、git、
# 一份钉死版本的 pytest 和几个常被 addopts 引用的插件、uv，以及两个**落在磁盘上**
# 的可写目录（HOME 和 TMPDIR）。
#
# 上面两层是 bench-env:{environment_id}（装某个仓库的依赖）和
# bench-agent:{env}-{agent}（装被测 AI 的 CLI）。
#
# 建：make images-base  或  python -m cli.images build --base
FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534
# ↑ 就是 python:3.11-slim（2026-09-08 解析）。**按 digest 引用，不按 tag**：
#   协议 C-36 说的是 tag 会被覆盖、digest 不会，这一条对 FROM 同样成立 ——
#   底座悄悄换了版本，上面所有 env 镜像的依赖解析结果都可能跟着变，而没人会注意到。
#   换基础镜像时改这一行，并同步 images.BASE_IMAGE_REF。
#   （这台机器用的是 containerd 镜像存储，`docker image inspect` 报的 Id 就是
#    manifest digest，`docker pull python@sha256:9534…` 实测能解析，不是本地假 digest。）

# ── 系统包 ────────────────────────────────────────────────────
#
# apt 走国内镜像源，而且**明确绕开代理**。
#
# dockerd 会把它自己那份代理配置注进每一个构建步骤（2026-09-08 实测：构建步骤里
# `env | grep -i proxy` 打得出 HTTP_PROXY，用 docker SDK 触发的构建也一样）。
# 走它拉 deb.debian.org 是 9.2 秒一个请求，直连 1.4 秒 —— images/claude-code 那份
# Dockerfile 第一次构建因此挂了 14 分钟一个包都没下完。
#
# 装的是"编译 C 扩展要用的东西"：真实仓库的依赖里少不了要现场编译的轮子。
# 不往里堆具体仓库才需要的库（libpq、libxml2 之类），那些写进 env 配方的
# apt_packages，让"这个仓库比别人多要什么"看得见。
ARG APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
RUN sed -i "s|http://deb.debian.org/debian|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        apt-get update \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        apt-get install -y --no-install-recommends \
            build-essential git curl ca-certificates pkg-config \
            libffi-dev libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Python 工具 ───────────────────────────────────────────────
#
# pip 走国内镜像源。**不是可选项**：这台机器直连 PyPI 会大面积超时
# （`ReadTimeoutError` / `SSL: UNEXPECTED_EOF`），表现成"这个包装不上"，
# 而它其实只是没拉到（`03-benchmark-spec.md` §8.8 坑 ④，风险 R18）。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# pytest 钉死 9.1.1，和 images/golden/Dockerfile 是同一个钉子：报告解析器
# （app/judge/report_parser.py）的 fixture 是按这个版本的 junitxml 录的，
# 版本一漂，解析结果会悄悄变化而 CI 不会有任何提示。
#
# 四个插件不是凑数：很多仓库在 pytest.ini / pyproject.toml 的 addopts 里写死了
# `--cov-report` 之类的参数，插件不在就直接 `error: unrecognized arguments`、
# 退出码 4，**一条用例都跑不了**（§8.8 坑 ⑥，python-pinyin 实测）。
#
# setuptools + wheel 也是这一层的事：只有 setup.py 的老项目在 pip 的构建隔离里
# 会报 "setuptools is not available in the build environment"（§8.8 坑 ⑦）。
# 它是基础镜像该有的东西，不是某个仓库的依赖负担。
#
# uv：nonebot2 那一类仓库的构建后端是 uv-build，pip 装不上（§8.8 坑 ⑧）。
# pip 自己也要钉、也要升。
#
# python:3.11-slim 自带的是 pip 24.0，它**不认 PEP 735 的 `[dependency-groups]`**
# （要 25.1 才有 `--group`）。tortoise-orm 的测试依赖全写在那儿，用 24.0 装完
# `pip install -e .` 会一切正常，然后 `pytest --collect-only` 72 个模块 import 失败 ——
# 一个"装上了但一条用例都跑不了"的环境（2026-09-08 实测，被建完自查当场拦下）。
RUN pip install --no-cache-dir pip==26.2.1

RUN pip install --no-cache-dir \
        pytest==9.1.1 \
        pytest-cov pytest-asyncio pytest-mock pytest-timeout \
        setuptools wheel uv

# ── HOME 和 TMPDIR：两个都要落在磁盘上 ─────────────────────────
#
# 这是 §8.8 坑 ⑤，那一条踩了两次。
#
# 评测容器跟着**宿主机的 uid** 跑（sandbox.container.default_container_user），
# 这个 uid 在镜像的 /etc/passwd 里没有条目，于是 docker 把 HOME 设成 `/`，而 `/`
# 不可写。pip 装包时写不进系统 site-packages 就会退回 user 安装、往 `$HOME/.local`
# 写；HOME 指向 `/tmp` 的话，装的包全进内存 —— `/tmp` 在评测容器里是 tmpfs，
# 吃的是内存额度，装 torch 那一套直接 `No space left on device`。
#
# TMPDIR 同理，而且**只挪一个不够**：先把 TMPDIR 挪到磁盘上，LlamaFactory 仍然
# 栽在 HOME 上（2026-09-08 实测）。
#
# 权限给 0777 是因为容器里那个 uid 事先不知道是几，镜像里没法按 uid 授权。
# 这不是安全边界 —— 真正兜底的是 cap_drop=ALL + no-new-privileges + 非 root +
# 测试阶段 --network none，这四条都在 sandbox.container 里。
RUN mkdir -p /home/bench /var/tmp/bench /opt/bench \
    && chmod 0777 /home/bench /var/tmp/bench /opt/bench
ENV HOME=/home/bench \
    TMPDIR=/var/tmp/bench \
    XDG_CONFIG_HOME=/home/bench/.config \
    XDG_CACHE_HOME=/home/bench/.cache \
    PATH=/home/bench/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# ── 建镜像时的自查工具 ────────────────────────────────────────
#
# bench-env 建完之后要验一件事：`import 某个包`拿到的是不是挂进来的工作区那一份。
# 拿到镜像里 /opt/repo 那一份的话，被测 AI 改的代码根本不会被执行 ——
# 测试照跑、可能还全绿，而 Oracle 哨兵会莫名其妙掉下去。
# 详见 images.smoke_check() 的注释。
COPY bench_import_check.py /usr/local/bin/bench-import-check
RUN chmod 0755 /usr/local/bin/bench-import-check

# 评测时工作区绑定挂载到这里，由执行器指定 workdir。
WORKDIR /workspace
