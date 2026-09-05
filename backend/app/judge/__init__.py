"""判定引擎：测试报告解析、F2P/P2P 判定。判定必须完全确定，禁止使用大模型。

**补丁归一化不在这里**，在 `app.runner.patch`（E3-T3）。补丁捕获属于 Runner 的活，
而且 import-linter 有一条"judge 不依赖 runner"—— 放这儿会一写就红。
`06-judge-attribution.md` §11.4 把它和判定写在同一章，那是按主题分的章节，不是按包分的。

依赖规则：可依赖 storage / infrastructure / domain。

**看不到 sandbox** —— import-linter 的分层里 `app.sandbox | app.judge | app.attribution`
是并排的，并排就是互不可见（2026-09-05 实测确认）。所以"打补丁、起容器、跑测试"
那一串在 `app.evaluation.executor`（E4-T2），它在 runner 之上，两边都看得见。
judge 这边只做纯粹的解析和判定，不碰任何外部世界。
"""
