# Golden 题的测试镜像（E4-T2 的临时方案）。
#
# 为什么需要它：测试阶段必须断网（协议 C-31），容器里装不了依赖；而按 ADR-008，
# 装依赖本来就该在建镜像时做掉，不是每次评测做一遍。
#
# 为什么是"临时"：真正的镜像分层构建器是 E2-T3（bench-base / bench-env / bench-agent
# 三层 + digest 记录 + 缓存 + 回收），它不在 M1 关键路径上，往后排了。这份 Dockerfile
# 只解决一件事：让 Golden 题的测试容器里有 pytest。E2-T3 到位之后，
# 测试执行器换个镜像名就行，代码一行不用改 —— 它只收 `image` 参数，不关心镜像哪来的。
#
# 建：make images
FROM python:3.11-slim

# 钉死版本：pytest 的输出格式（junitxml 的 family、短摘要的写法）跨版本会变，
# 而报告解析器的 fixture 是按某个具体版本录的。不钉的话，某天基础镜像里的 pytest
# 升级了，解析结果会悄悄变化，而 CI 不会有任何提示。
RUN pip install --no-cache-dir pytest==9.1.1

# 评测时工作区绑定挂载到这里，由测试执行器指定 workdir。
WORKDIR /workspace
