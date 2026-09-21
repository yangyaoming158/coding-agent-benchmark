# 部署文档

> 一句话：装好 Docker，`git clone`，在 `.env` 里填一个管理令牌，`make compose-up`，五个服务就起来了。
> 再跑一次 `make compose-smoke`，用四道 Golden 题证明评测链路是真的通，不只是进程起来了。

**这份文档给谁看**：没参与过开发、要在一台干净机器上把平台装起来的同学。读它不需要懂平台内部。
装好之后怎么用见 [`usage.md`](usage.md)，为什么这么设计见 [`architecture.md`](architecture.md)。

**每条命令都在 2026-09-21 于开发机上原样跑过一遍**。跑的地方是一份仓库复制目录
`/home/oslab/projects/bench-deploy-test 部署验证`（路径故意带空格和中文），里面没有 Python 虚拟环境、没有 node_modules，
`.env` 只比样板多填了管理令牌、把三个端口改成 8001 / 3001 / 5434、项目名改成 `benchtest`（免得和同一台机器上的开发环境撞）。
所以下面回显里的端口是 8001 / 3001 / 5434；你按默认装出来是 8000 / 3000 / 5433。

---

## 1. 装完是什么样

`make compose-up` 起五个容器，外加一个只在你敲命令时才起的 `cli`：

| 服务 | 镜像 | 干什么 | 对外端口 |
|:---|:---|:---|:---|
| `postgres` | `postgres:16-alpine` | 数据库。17 张表、作业队列都在里面（不用 Redis） | 5433 |
| `migrate` | `bench-platform` | 一次性：`alembic upgrade head` 建表 / 升表，跑完就退出 | — |
| `api` | `bench-platform` | FastAPI 后端，`/api/*` 和 `/docs` | 8000 |
| `worker` | `bench-platform` | 从队列里领题、起评测容器、判定、落库 | **无**（见 §5.4） |
| `frontend` | `bench-frontend` | Next.js 前端 | 3000 |
| `cli`（按需） | `bench-platform` | `make compose-cli CMD="python -m cli.…"` 时临时起一个跑命令，跑完删 | — |

```
浏览器 ──→ frontend:3000 ──→ api:8000 ──→ postgres:5432
                                            ↑
                           worker ──────────┘ 领作业、写结果
                             │
                             └── /var/run/docker.sock ──→ 宿主机 dockerd ──→ 评测容器（Agent 容器、测试容器）
```

**一件和别的项目不一样的事：代码不在镜像里。** `bench-platform` 镜像只装了 Python 依赖，
仓库目录按**宿主机上的原路径**挂进 `api` / `worker` / `cli` 三个容器。
原因是 Worker 起评测容器用的是宿主机的 dockerd（上图最后一行），它把工作区路径原样交给 dockerd，dockerd 按**宿主机**的文件系统去找 ——
容器里的路径和宿主机不一样，dockerd 就会在宿主机上新建一个同名空目录挂进去，评测容器看到的是空工作区，而且不报错。
细账在 `docker-compose.yml` 顶部注释和 `docs/plan/05-sandbox.md` §10.6。对你的影响只有两条：

- 改代码不用重建镜像，重启容器就生效；只有 `backend/pyproject.toml` / `backend/uv.lock` 变了才要重建。
- 仓库的绝对路径要传给 compose（环境变量 `BENCH_REPO_DIR`）。`make compose-*` 会自动传，你不用管；
  只有绕开 `make` 直接敲 `docker compose` 时才要自己写进 `.env`。

---

## 2. 前提

### 2.1 操作系统：Linux，或 Windows + WSL2

原生 Windows 跑不了（起容器的代码用了 `os.getuid()`，Windows 上没有这个函数），
理由和怎么办见 [`CONTRIBUTING.md`](../CONTRIBUTING.md) 第 1 节。用 WSL2 的话有两件事先确认：

- `/etc/wsl.conf` 里有 `[boot]` `systemd=true`，不然 dockerd 不会开机自启（开发机就是这么配的）。
- Windows 用户目录下的 `.wslconfig` 决定 WSL 能用多少 CPU 和内存。开发机是 `memory=12GB` / `processors=16`。
  评测容器每个限额 1–2 GB（题目默认测试容器 2 GB、Agent 容器 1 GB），内存太小并发就要往下调（见 §2.3）。

### 2.2 要装的软件

| 软件 | 最低要求 | 怎么确认 | 开发机上 |
|:---|:---|:---|:---|
| Docker Engine（**原生**，不是 Docker Desktop，见 §5.2） | 能免 sudo 跑 `docker ps` | `docker --version` | 29.7.2 |
| docker compose 插件 | **≥ 2.20**（用了 `depends_on.condition: service_completed_successfully` 和 `up --wait`） | `docker compose version --short` | 5.5.0 |
| git | ≥ 2.32 | `git --version` | 2.43.0 |
| GNU make | 任意 | `make --version` | 4.3 |
| python3 | 3.10+，只用来跑自检脚本 | `python3 --version` | 3.12.3 |

**不需要**装 uv、Node、Python 依赖 —— 它们全在容器里。

Docker 按[官方文档](https://docs.docker.com/engine/install/ubuntu/)装 `docker-ce` `docker-ce-cli` `containerd.io` `docker-buildx-plugin` `docker-compose-plugin` 这五个包，
装完把自己加进 docker 组（`sudo usermod -aG docker $USER`，重新登录生效）。
开发机是 2026-09-01 装的，这次没有重装，**装 Docker 这一步没有复测**，验收时如果撞到问题请记进 §9。

### 2.3 硬件

开发机是 16 vCPU / 11.7 GiB（WSL 里看到的）/ 磁盘余 794 GiB。自检脚本在少于 8 核或 8 GiB 时会告警（不阻塞）。

磁盘：部署本身要下约 3 GB 镜像（`bench-platform` 527 MB、`bench-frontend` 1.45 GB、`postgres:16-alpine` 420 MB、
`python:3.11-slim` 200 MB、Golden 冒烟用的 `bench-golden` 232 MB，都是 `docker images` 显示的未去重数字）。
之后每接一个开源仓库要建一个环境镜像，几百 MB 到 10 GB 不等（`CONTRIBUTING.md` §1.5 有清单）。
`/var/lib/docker` 所在分区剩余低于 15%（`.env` 的 `IMAGE_DISK_MIN_FREE_RATIO`）时平台会拒绝建镜像、暂停领新作业。

### 2.4 网络

建镜像和起服务要能访问这几处：

| 用途 | 地址 | 拉不到时 |
|:---|:---|:---|
| 基础镜像 `python:3.11-slim`、`postgres:16-alpine` | Docker Hub（或它的国内镜像站，§5.1 第二行） | 见 §6.2 |
| 后端 Python 依赖、apt 包 | `mirrors.aliyun.com`（默认，可用 build-arg 换） | 见 §6.1 |
| 前端 Node 二进制、npm 包 | `npmmirror.com` | 同上 |

如果机器要过代理才能出网，先按 §5.1 把三处代理配好，**否则第 4 步一定卡在拉镜像**。

---

## 3. 部署步骤

### 第 1 步：取代码

```bash
git clone https://github.com/yangyaoming158/coding-agent-benchmark.git
cd coding-agent-benchmark
```

目录名随便，带空格和中文也行（开发机的仓库路径就是 `…/coding agent 基准测试平台`），但后面所有命令里的路径都要**加引号**。

### 第 2 步：写 `.env`

`.env` 已被 `.gitignore` 排除，不会被提交。它里面唯一**必填**的是管理令牌 `ADMIN_TOKEN`：

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

把第二条打出来的那串填到 `.env` 的 `ADMIN_TOKEN=` 后面（用编辑器改，或者单行 `sed -i "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=把那串贴这里|" .env`）。

为什么必填：后端的写接口（建实验、取消、补跑）靠它把门，请求要带 `X-Bench-Token` 头。**没填的话 API 进程会拒绝启动**，
而 compose 在起容器之前就会当场报错（实测回显）：

```
$ BENCH_REPO_DIR="$PWD" ADMIN_TOKEN= docker compose config
error while interpolating services.api.environment.ADMIN_TOKEN: required variable ADMIN_TOKEN is missing a value: .env 里没填 ADMIN_TOKEN。生成一个：python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

其他项现在都不用动：

- 各家大模型的 Key（`DEEPSEEK_API_KEY` 等）只在跑真实 AI 时才要，Golden 冒烟不需要。
- 三个端口 `BENCH_API_PORT` / `BENCH_WEB_PORT` / `BENCH_PG_PORT` 默认 8000 / 3000 / 5433，被占了就在这里改（见 §5.3）。
- `BENCH_REPO_DIR` 留空。`make compose-*` 会自动填当前目录。
- `BENCH_DATABASE_URL` 那一行是给宿主机上的开发工具用的，容器里 compose 会覆盖成 `postgres:5432`，不用改。

### 第 3 步：环境自检

```bash
python3 scripts/check_env.py
```

它把开发过程中踩过的坑固化成了检查项。在那份验证副本上的回显：

```
=== 开发环境自检 ===

✅ Docker 命令存在: /usr/bin/docker
✅ Docker daemon 可用（免 sudo）: 29.7.2
✅ cgroup v2: v2
✅ 连的是原生引擎而非 Docker Desktop: 原生引擎
✅ daemon 配了代理: http://172.30.80.1:10808
✅ 配了 registry mirrors: https://docker.1ms.run/
✅ 资源够跑并发: 16 核 / 11.7 GiB
✅ docker compose ≥ 2.20: 5.5.0
✅ git ≥ 2.32: 2.43.0
✅ 测试镜像 bench-golden:py311 已构建: 已构建
✅ 环境镜像底座 bench-base:py311 已构建: 已构建
✅ 镜像分区余量: /var/lib/docker 剩 793.9 GiB（78.9%）
⚠️  后端依赖已安装: backend/.venv 不存在
     → 先跑 make install
✅ git 工作区干净: 干净

✅ 全部通过（⚠️ 项不阻塞，但建议处理）
```

干净机器上的差别：`bench-golden` / `bench-base` 两行会是 ⚠️（镜像还没建，冒烟那一步会自动建 `bench-golden`）；
机器直连出网的话代理那两行也是 ⚠️。`backend/.venv 不存在` 是正常的 —— compose 部署不需要它，那是给开发模式的提示。
⚠️ 都不阻塞；出现 ❌ 的只会是 Docker 本身（命令不存在、daemon 连不上、不是 cgroup v2、连到了 Docker Desktop）或磁盘余量，修好再往下走。

### 第 4 步：一键起服务

```bash
make compose-up
```

它做三件事：建两个镜像（`bench-platform` 后端运行时、`bench-frontend` 前端，两个**逐个**建，理由见 §6.3）、
`docker compose up -d --wait`、等到五个服务全部 healthy 后打印地址。

**第一次要建镜像，慢**：E10-T1 在这台机器上从零建到全部 healthy 用了 1 分 49 秒（依赖全从国内源下）；
镜像已经建过时（本次实测）19 秒。收尾的回显是：

```
 Container benchtest-postgres-1 Healthy
 Container benchtest-migrate-1 Exited
 Container benchtest-frontend-1 Healthy
 Container benchtest-api-1 Healthy
 Container benchtest-worker-1 Healthy
API   http://localhost:8001/docs
前端  http://localhost:3001
下一步：make compose-smoke 跑一遍 Golden 冒烟；平台命令用 make compose-cli CMD="python -m cli.seed"
```

`migrate` 显示 `Exited` 是对的 —— 它是一次性任务，建完表就退出；退出码非 0 的话 `api` / `worker` 根本不会起。

### 第 5 步：确认真的起来了

```bash
make compose-ps
curl -s http://localhost:8000/api/health
```

回显（端口按副本的 .env 是 8001 / 3001 / 5434）：

```
NAME                   IMAGE                   COMMAND                  SERVICE    CREATED          STATUS                    PORTS
benchtest-api-1        bench-platform:latest   "bench-entrypoint uv…"   api        33 seconds ago   Up 25 seconds (healthy)   0.0.0.0:8001->8000/tcp, [::]:8001->8000/tcp
benchtest-frontend-1   bench-frontend:latest   "npm run start"          frontend   34 seconds ago   Up 33 seconds (healthy)   0.0.0.0:3001->3000/tcp, [::]:3001->3000/tcp
benchtest-postgres-1   postgres:16-alpine      "docker-entrypoint.s…"   postgres   34 seconds ago   Up 33 seconds (healthy)   0.0.0.0:5434->5432/tcp, [::]:5434->5432/tcp
benchtest-worker-1     bench-platform:latest   "bench-entrypoint py…"   worker     33 seconds ago   Up 25 seconds (healthy)

{"status":"ok","protocol_version":"v1.2","database":"ok","migration_revision":"0008"}
```

看三处：四个常驻服务都是 `(healthy)`；`worker` 那一行 PORTS 是空的（故意的，§5.4）；
`/api/health` 里 `database` 是 `ok`、`migration_revision` 是当前最新的迁移号（现在是 `0008`，以后加了迁移会变）。

浏览器打开 `http://localhost:3000` 是前端，`http://localhost:8000/docs` 是后端的接口文档（Swagger UI）。
Windows + WSL2 的话直接在 Windows 的浏览器里开 `localhost` 就行，WSL2 默认把 localhost 转发进发行版。

### 第 6 步：冒烟 —— 证明评测链路真的通

```bash
make compose-smoke
```

它用四道 Golden 题（仓库里自带的、人工写的小题，`datasets/golden/`）跑两个"哨兵"：
**Oracle** 交官方补丁，四道题必须全部判 RESOLVED；**Noop** 交空补丁，必须一道都不 RESOLVED。
这一趟走完，等于证明了 Worker 在容器里真能通过 docker.sock 起评测容器、工作区路径没走样、判定链路完整。
六步：建 `bench-golden` 测试镜像（已有就跳过）→ Golden 题生成 git 镜像、写入哨兵 Agent、题目入库 → 八步验证四道题 →
冻一版数据集快照 → 建 Oracle / Noop 两个实验 → 等 Worker 跑完。本次实测 **50 秒**，回显的尾部：

```
✓ bench-golden__auth-2               VALID
✓ bench-golden__cart-3               VALID
✓ bench-golden__pager-4              VALID
✓ bench-golden__textkit-1            VALID
共 4 道，VALID 4，其余 0
── 4/6 冻快照 golden（已有且内容一样时是 noop）
golden@v1（刷新草稿 v1）
  来源 dataset_id  golden-v1
  冻进快照        4 道题
  快照摘要        sha256:7fde023f6dcaccca0fdc1f1844fb54d5c97f8d0649e7cea7229bba1a598aad5f
── 5/6 建两个实验：Oracle（官方补丁，应 4/4）和 Noop（空补丁，应 0/4）
实验 #1（compose-smoke-oracle）已建，投了 4 条 EVAL_TASK 作业
实验 #2（compose-smoke-noop）已建，投了 4 条 EVAL_TASK 作业
── 6/6 等 Worker 跑完（实验 #1 / #2）
Oracle #1: COMPLETED  解决 4/4  平台故障 0  dirty=false
Noop   #2: COMPLETED  解决 0/4  平台故障 0  dirty=false
✅ 冒烟通过：Worker 在容器里能起评测容器，判定链路完整
细账：make compose-cli CMD="python -m cli.experiment status --run 1"
```

中间会有一行 `⚠ golden 还没有已发布的版本，用的是草稿 v1（没过门禁）`，正常 —— 冒烟不需要走发布流程。

`dirty=false` 表示建实验时仓库工作区是干净的（`git status --porcelain` 为空）。**如果你改过仓库里的文件再跑冒烟，
这一步会被协议 C-27 拒绝**，加 `ALLOW_DIRTY=1 make compose-smoke` 放行，结果会标 `dirty=true`，只能当冒烟、不能当成绩。

**⚠ 别在开发机的主仓库跑这条。** compose 起的是一个新库，实验号从 1 开始编，制品会写进宿主机 `var/artifacts/runs/1/`，
和开发库里已有的 1 号实验混在一起。只在干净目录（比如这次的验证副本）里跑。

到这里部署完成。接下来看 [`usage.md`](usage.md)。

---

## 4. 日常操作

| 要做什么 | 命令 | 备注 |
|:---|:---|:---|
| 看五个服务的状态 | `make compose-ps` | |
| 看日志 | `make compose-logs SERVICE=worker` | 跟随模式，Ctrl-C 退出；不给 `SERVICE=` 就是全部 |
| 跑一条平台命令 | `make compose-cli CMD="python -m cli.experiment status"` | 在临时容器里跑，跑完容器自动删；命令的工作目录是 `backend/`，所以仓库里其他文件写 `../datasets/…` |
| 停掉 | `make compose-down` | 删容器、**保留数据**（数据库在命名卷 `pgdata` 里，`var/` 在宿主机） |
| 再起来 | `make compose-up` | 实测停了再起，之前的实验都还在 |
| 更新代码 | `git pull && make compose-down && make compose-up` | 代码是挂进来的，不用重建镜像；`pyproject.toml` / `uv.lock` 变了 `compose-up` 自带的 build 会重建；新加的数据库迁移由 `migrate` 自动跑 |
| 彻底删掉 | `BENCH_REPO_DIR="$PWD" docker compose down -v`，再手动删 `var/` | `-v` 连数据库卷一起删，**不可恢复**；`var/` 里的工作区、制品、git 镜像 compose 不管，要自己删 |

实测 `make compose-down` 再 `make compose-up`（22 秒）之后：

```
$ make compose-cli CMD="python -m cli.experiment status"
   #  Agent      状态                进度      解决    故障         成本  名字
   2  noop       COMPLETED        4/4     0/4     0     0.0000  compose-smoke-noop
   1  oracle     COMPLETED        4/4     4/4     0     0.0000  compose-smoke-oracle
```

**停 Worker 要有耐心。** 它收到停机信号后会把手上那道题跑完再退（最多等 20 分钟，`.env` 的 `WORKER_SHUTDOWN_GRACE_S`），
compose 里给它的宽限期是 21 分钟。所以 `make compose-down` 在有题在跑时可能要等几分钟，这是正常的 ——
强杀会让评测容器变孤儿、作业要等租约过期才会被重新领走。等不及就
`BENCH_REPO_DIR="$PWD" docker compose kill -s SIGTERM worker` 再发一次信号，第二次的意思是"不等了"，它仍会回收容器。

**一台机器只跑一个 Worker。** compose 的 `worker` 已经在跑时，不要再在宿主机上 `make worker`。
第二个 Worker 启动时的孤儿容器回收会把第一个正在用的评测容器当孤儿杀掉，被杀的那道题看起来像一次内存超限
（`AGENTS.md` 第 10 节）。同一个库上的第二个 Worker 会被数据库锁挡住反复重启，`make compose-ps` 里显示 unhealthy。

---

## 5. 部署前必读的四条约束

这四条是 `docs/plan/05-sandbox.md` §10.6 点名要写进部署文档的。每一条都在开发机上真的撞过。

### 5.1 代理要配三处，少一处就"拉不动镜像"

开发机的网络要经 Windows 侧的代理出网，而 **dockerd 不读 shell 里的 `HTTP_PROXY`**，三处要分别配：

| 配置点 | 文件 | 管什么 | 开发机上的内容 |
|:---|:---|:---|:---|
| dockerd 自己的代理 | `/etc/systemd/system/docker.service.d/http-proxy.conf` | `docker pull` 走代理 | `[Service]` 下三行 `Environment="HTTP_PROXY=http://172.30.80.1:10808"`、`HTTPS_PROXY` 同值、`NO_PROXY=localhost,127.0.0.1,::1,.local,docker.1ms.run` |
| 镜像加速 | `/etc/docker/daemon.json` 的 `registry-mirrors` | Docker Hub 的镜像走国内镜像站，不占代理带宽 | `{"registry-mirrors":["https://docker.1ms.run","https://docker.m.daocloud.io"]}` |
| 容器内代理 | `~/.docker/config.json` 的 `proxies.default` | `docker build` / `docker run` 时把代理注进容器，供 pip 装包、被测 AI 访问大模型接口 | `httpProxy` / `httpsProxy` 都是 `http://172.30.80.1:10808`，`noProxy` 是 `localhost,127.0.0.1,::1` |

改前两处要 `sudo`，改完 `sudo systemctl daemon-reload && sudo systemctl restart docker`。写文件用单行命令，比如
`echo '{"registry-mirrors":["https://docker.1ms.run","https://docker.m.daocloud.io"]}' | sudo tee /etc/docker/daemon.json >/dev/null`
—— 别用多行 heredoc 粘贴，开发机上出过一次结束符没生效、命令文本被写进配置文件、dockerd 起不来的事。

代理地址是 WSL 的网关 IP，用 `ip route show default` 取（开发机现在是 `172.30.80.1`），**`wsl --shutdown` 之后可能变**，
变了三处要一起改。自检脚本的"daemon 配了代理"和"配了 registry mirrors"两行就是查这个。

平台自己的两个 Dockerfile（`deploy/backend/`、`deploy/frontend/`）在 apt / pip / npm 那几步**故意绕开**这个代理，直连国内镜像源 ——
dockerd 会把上面第三处的代理注进每个构建步骤，让国内源走境外代理只会慢十倍。

### 5.2 和 Docker Desktop 共存

一台机器上只能有一个 Docker daemon 在管 `/var/run/docker.sock`。开发机的 Windows 侧也装了 Docker Desktop（别的项目用），二者共存的硬性前提：

| 约束 | 为什么 |
|:---|:---|
| **不要在 Docker Desktop 里为这个 WSL 发行版开启集成** | 开了之后它把自己的 CLI 前置进 PATH、接管 `docker.sock`，docker 命令连到另一个 daemon 上。症状是镜像和容器"凭空消失"、正在跑的评测整体失败，排查成本极高 |
| **跑正式实验前退出 Docker Desktop** | 所有 WSL 发行版共用 `.wslconfig` 里的内存额度，Desktop 开着会占掉留给评测容器的那份 |
| 端口避让 | compose 发布的 8000 / 3000 / 5433 要避开 Desktop 侧项目在用的端口 |
| 自检 | `docker context show` 应是 `default`（别切到残留的 `desktop-linux`）；`docker info --format '{{.Name}} {{.DockerRootDir}}'` 应是本机名 + `/var/lib/docker`。开发机回显：`DESKTOP-D3QQNH3 /var/lib/docker` |

自检脚本那行"连的是原生引擎而非 Docker Desktop"查的是 `docker info` 的 Labels 里有没有 `com.docker.desktop.*`
（光看 `DockerRootDir` 分不出来，Desktop 报的也是 `/var/lib/docker`）。

### 5.3 端口避让

compose 默认发布 **8000**（API）、**3000**（前端）、**5433**（数据库）三个端口，都能在 `.env` 里改
（`BENCH_API_PORT` / `BENCH_WEB_PORT` / `BENCH_PG_PORT`）。两条要知道的：

- 数据库用 5433 不是 5432，是因为开发机上还有别的项目在用 Postgres 的默认端口。抢端口的报错信息看不出是端口冲突，
  表现是 `Error response from daemon: … port is already allocated` 或两边都起不来。
- **5433 和 `make db-up` 起的开发库 `bench-postgres` 是同一个端口，两个不能同时起。** 想在一台机器上同时跑开发模式和 compose，
  改 compose 这边的 `BENCH_PG_PORT`（这次验证副本就是改成 5434 才能和开发机上跑着的 `bench-postgres` 共存）。
- 改了 `BENCH_WEB_PORT` 或 `BENCH_API_PORT`，后端的跨域放行名单会自动跟着变；但 `NEXT_PUBLIC_API_BASE`（浏览器用哪个地址找后端）
  是**构建时**写死进前端页面的，改了 API 端口要同时改它并重建前端镜像（`make compose-build`）。

### 5.4 DooD：Worker 拿着宿主机的 root

Worker 要起评测容器，用的是**宿主机的 dockerd**（挂了 `/var/run/docker.sock`，这种做法叫 DooD，Docker outside of Docker；
不是在容器里再装一个 dockerd）。**拿着这个 socket 等于拿着宿主机的 root**：能起任意特权容器、挂任意宿主目录。
这在单机实训环境里可以接受，但必须知道并且做了三件事收窄：

1. `worker` **不发布任何端口**，外面连不到它（§3 第 5 步回显里 PORTS 那列是空的）。`api` 不挂 socket，它是唯一对外的后端进程。
2. 只有 `worker` 和按需起的 `cli` 挂 socket。`docker-compose.yml` 里就这两处，`backend/tests/unit/test_compose_deploy.py` 有测试盯着。
3. Worker 进程**不以 root 跑**。容器入口脚本（`deploy/backend/entrypoint.sh`）读仓库目录的属主，切成那个 uid/gid 再启动
   （可用 `.env` 的 `BENCH_UID` / `BENCH_GID` 覆盖）。这样 `var/` 下写出来的文件归你，不用 sudo 才能删；
   经它起的评测容器仍是非 root、`cap_drop=ALL`、测试阶段断网 —— 和在宿主机上直接跑 Worker 一模一样，部署方式没改沙箱策略。

怎么确认第 3 条（实测）：

```
$ BENCH_REPO_DIR="$PWD" docker compose exec worker sh -c 'grep -E "^(Uid|Gid|Groups)" /proc/1/status'
Uid:    1000    1000    1000    1000
Gid:    1000    1000    1000    1000
Groups: 1000 1001
```

1000 是仓库属主，1001 是 `docker.sock` 的属组（宿主机的 docker 组）。注意 `docker compose exec worker id` 会显示 root ——
`exec` 起的是一个新进程，不经过入口脚本；要看的是 1 号进程。

评测容器本身的隔离（非 root、能力全丢、进程数限制、内存限制、断网）不是 compose 管的，是 Worker 起容器时设的，
E10-T1 验过部署后这些都没变：评测容器 `uid=1000`、`CapEff` 全零、`NoNewPrivs=1`、只有 `lo` 网卡（`05-sandbox.md` §10.6 回填）。

---

## 6. 这台机器上撞过的网络坑

都不是代码的问题，但换一台校内网络的机器很可能再撞。

### 6.1 清华源 403、USTC 429

2026-09-21 实测：清华的 debian 和 pypi 两个源当天都返回 403；中科大的 pypi 对 uv 的并发下载限流（429）；阿里云两样都正常。
所以平台的两个 Dockerfile **默认阿里云**，要换源不用改文件，传 build-arg：

```bash
BENCH_REPO_DIR="$PWD" docker compose build --build-arg APT_MIRROR=https://mirrors.ustc.edu.cn/debian --build-arg PIP_INDEX_URL=https://mirrors.ustc.edu.cn/pypi/simple api
```

顺带一条：后端镜像不能直接 `uv sync --frozen`，因为 `uv.lock` 里记的是**绝对下载地址**（清华源），`--default-index` 换不掉。
现在的做法是构建时 `uv export` 导出"名字==版本 + sha256"再从上面那个源装，哈希照样校验。这是 Dockerfile 里已经处理好的，提一下是免得有人"优化"回去。

**评测用的环境镜像（`images/base/Dockerfile`、`images/envs/*.json`）还是清华源**，建那些镜像时会撞同一个 403，怎么改见 `usage.md` §2。

### 6.2 Docker Hub 拉不动

2026-09-21 实测 `docker pull node:24-alpine` / `python:3.12-slim` 过代理 10 分钟拉不到 6 MB。所以：

- 前端镜像的底座改成了 `python:3.11-slim`（凡是建过评测镜像的机器上都已经有它），Node 从 npmmirror 下 tar 包（31 MB，12 MB/s，SHA256 和官方一致）。
- 部署真正要从 Docker Hub 拉的只剩 `python:3.11-slim` 和 `postgres:16-alpine`。先配好 §5.1 的镜像加速再拉。
- 实在拉不到：从一台有这两个镜像的机器导出再导入。单行：
  `docker save python:3.11-slim postgres:16-alpine | gzip > base-images.tgz`，拿到目标机器 `gunzip -c base-images.tgz | docker load`。
  开发机上这两个镜像 `docker images` 显示 200 MB + 420 MB。（这条是备用方案，这次没有走到。）

### 6.3 路径含中文时 buildx 一条命令建两个镜像会报错

`docker compose build` 一次建 `api` 和 `frontend` 两个镜像时，buildx 0.36.1 在仓库路径含非 ASCII 字符（比如中文）时报
`header key "x-docker-expose-session-sharedkey" contains value with non-printable ASCII characters`；纯 ASCII 路径正常。
`make compose-build` 是逐个建的，绕开了；`make compose-up` 也因此不带 `--build`。**如果你手敲 `docker compose up --build` 撞到这条，改用 `make compose-up`。**

---

## 7. 排错速查

| 症状 | 原因 | 怎么办 |
|:---|:---|:---|
| `required variable BENCH_REPO_DIR is missing a value: 没有 BENCH_REPO_DIR…` | 绕开 `make` 直接敲了 `docker compose up` / `run` / `config`（`ps` 不会报） | 用 `make compose-*`；或在 `.env` 里写 `BENCH_REPO_DIR=<仓库绝对路径>` |
| `required variable ADMIN_TOKEN is missing a value: .env 里没填 ADMIN_TOKEN…` | 第 2 步没填 | 按第 2 步生成并填入 |
| `Bind for 0.0.0.0:5433 failed: port is already allocated` | 端口被占，最常见是开发库 `bench-postgres` 在跑 | `.env` 里改 `BENCH_PG_PORT`（8000 / 3000 同理） |
| `make compose-up` 卡在拉 `python:3.11-slim` / `postgres:16-alpine` | 代理或镜像加速没配 | §5.1；实在不行 §6.2 的导出导入 |
| 建镜像时 apt / pip 报 403 / 429 / 超时 | 镜像源当天不可用 | §6.1 换源 |
| `docker compose build` 报 `x-docker-expose-session-sharedkey … non-printable ASCII` | 路径含中文，一次建两个镜像 | 用 `make compose-build`（§6.3） |
| `worker` 一直 unhealthy，日志里反复启动 | 同一个库上已有另一个 Worker 持有单实例锁（比如宿主机上 `make worker` 没停） | `ps -eo pid,args \| grep '[p]ython3 -m app.worker'` 找到并 `kill -TERM`；一台机器只跑一个 Worker |
| 冒烟 Oracle 不是 4/4，日志里评测容器报找不到文件 | 容器里的仓库路径和宿主机不一致（`BENCH_REPO_DIR` 填错） | 用 `make compose-up` 让它自动传；检查 `.env` 里有没有写错的 `BENCH_REPO_DIR` |
| 冒烟被拒：`工作区不干净` | 协议 C-27，`git status --porcelain` 不为空 | 提交或还原改动；只想冒烟就 `ALLOW_DIRTY=1 make compose-smoke` |
| 镜像和容器"凭空消失" | Docker Desktop 的 WSL 集成接管了 socket | §5.2；`sudo systemctl restart docker.socket docker.service` 把 socket 抢回来 |
| 评测全部 UNRESOLVED，被测 AI 像"一个文件都改不了" | Worker 以 root 跑、评测容器退到 nobody 写不进工作区 | 别设 `BENCH_UID=0`；确认 §5.4 第 3 条的 1 号进程 uid 不是 0 |

---

## 8. 和开发模式（`make dev`）的关系

开发用 `make dev`（宿主机上 uvicorn + `npm run dev`）+ `make db-up`（`scripts/dev_db.sh` 起的 `bench-postgres` 容器）+ `make worker`。
compose 是另一套编排，不是替代：仓库里**没有** `docker-compose.dev.yml`，两套编排只会各改各的。

两边共用的东西：同一份 `.env`、同一个 `var/`（工作区、制品、git 镜像都在宿主机目录里）。
不共用的：数据库（compose 是命名卷 `pgdata`，开发库是 `bench-postgres` 容器自己的卷），所以两边各有一套实验号。
宿主机上装了 uv 的话，`cd backend && uv run python -m cli.experiment status` 连的是 `.env` 里 `BENCH_DATABASE_URL` 指的那个库 ——
把它指到 compose 的端口就能用宿主机工具查 compose 的库（E10-T1 实测）。

---

## 9. 验收记录

DEL-06 的验收方式是"由**未参与开发的同学**照文档在干净环境部署成功"，这一步不能由作者代替。请部署的同学填这一节（直接改这个文件，提 PR）。

| 项 | 填写 |
|:---|:---|
| 部署人 / 日期 | |
| 机器（OS、CPU / 内存、Docker 与 compose 版本） | |
| 是否需要代理；§5.1 三处是否都改了 | |
| 第 3 步自检：❌ 有几项，分别是什么 | |
| 第 4 步 `make compose-up` 用时；是否一次成功 | |
| 第 5 步 `/api/health` 回显 | |
| 第 6 步 `make compose-smoke` 结果（Oracle x/4、Noop x/4、用时） | |
| 文档哪一步写得不对或看不懂（**最有价值的一栏**，请如实写） | |
| 结论：部署成功 / 失败（失败的话卡在哪一步、报错原文） | |
