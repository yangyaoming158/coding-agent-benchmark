# Golden Tasks

四道**手写**的评测题。不来自 GitHub 挖掘，不依赖网络，全部跑完只要几秒钟。

Week 1 的内核开发全靠它们：判定引擎、沙箱、Runner 适配器都需要一批"已知答案"的题来自测，
而真实题目要等挖掘器（E1-T4）就位。

| 题目 | 难度 | bug 是什么 | 考点 |
|:---|:---|:---|:---|
| `bench-golden__textkit-1` | easy | CSV 单行解析没处理引号里的逗号 | 把一个字符串扫描器写对 |
| `bench-golden__auth-2` | easy | `verify_password` 用 `or` 短路，空口令直接放行 | 看懂一个布尔表达式的短路行为 |
| `bench-golden__cart-3` | medium | 折扣用 `int()` 截断，每单少收一分 | issue 里说了两件事，不能只做一件 |
| `bench-golden__pager-4` | medium | `page_slice` 不校验页码，`page=0` 走进负数切片 | issue 明说"越界返回空列表不要改"，一刀切会打挂 P2P |

---

## 目录长什么样

```
datasets/golden/
    <task_id>.json          ← 生成物：完整的 TaskDefinition，可直接 cli.task import
    environments/           ← 生成物：一个环境规格一个文件，字段对齐 environment_specs 表
    sources/<task_id>/      ← 手写的源码，改题改这里
        task.toml             元数据：F2P / P2P 用例、难度、标签、环境规格
        issue.md              交给被测 AI 的 issue（第一行 `# 标题`，其余是正文）
        base/                 有 bug 的完整文件树，含仓库原有的测试
        fix/                  修复 PR 改动的文件（源码修复 + 新增的测试），覆盖到 base 上
```

**`*.json` 是生成的，不要手工改。** 改 `sources/` 再跑 `make golden`。
两边漂移了 `test_generated_json_matches_sources` 会让 CI 红。

环境规格单独放一个子目录，不和任务 JSON 挤在一起：`cli.task import datasets/golden/*.json`
是导入命令的标准用法，这个通配符里混进一个不是任务的 JSON，命令会当场拒收它，
看起来像题库里混进了坏题。

---

## `fix/` 里为什么源码和测试混在一起

因为真实世界的修复 PR 就是这样：既改代码又加测试。

`make golden` 会按受保护路径规则（协议 C-42）把这个 PR 的 diff 劈成两半 ——
碰测试文件的部分是 `test_patch`，其余是 `gold_patch`。这和 SWE-bench 从真实 PR
派生任务的做法一致，也顺带保证了两个补丁一定能干净地打上去。

---

## 上游仓库是生成出来的

Golden 题没有真实上游。`make golden` 用 `base/` 和 `fix/` 造一个两提交的仓库
（base → 修复），再 `git clone --mirror` 到 `var/mirrors/` 下。

提交人和时间都是写死的常量，所以 `base_commit` 在任何机器上都一样 ——
这是把 `base_commit` 写进版本库里那份 JSON 的前提。

`repo_url` 记成 `golden://<owner>/<repo>`，明确表示"这个仓库是生成的，没有上游可拉"。
换台机器之后 `var/mirrors/` 是空的（它在 `.gitignore` 里），跑一次 `make golden` 就有了。

---

## 六步验证

```bash
make golden-verify        # 或 cd backend && uv run python -m cli.golden verify
```

| 步骤 | 验的是什么 | 依据 |
|:---:|:---|:---|
| 1 | 物化：工作区历史只有一个提交，树哈希等于 base 树 | C-43 |
| 2 | 补丁体检：`gold_patch` 不碰受保护路径；`test_patch` 只碰测试文件；两者都能打上 | C-64、§7.1 |
| 3 | `base + test_patch` 上，**每条** F2P 都失败 | §7.2(5)，**Noop 解决率 0% 的依据** |
| 4 | `base + test_patch` 上，P2P 全部通过 | §7.2(6) |
| 5 | `base + test_patch + gold_patch` 上，F2P 全部通过 | §7.2(5)，**Oracle 解决率 100% 的依据** |
| 6 | 同上状态，P2P 仍然全部通过 | §7.2(6) |

第 3 步是逐条跑的，其余是整批跑。差别在于要证明的命题不同：整批跑只能得出
"至少挂了一条"，而第 3 步要证明的是**每一条**都挂 —— 漏掉一条在 base 上就通过的 F2P，
Noop 哨兵就会给出非零解决率。

**验证时是在本机直接起 pytest 子进程，不是沙箱执行器。** 这里跑的是我们自己写的代码，
没有不可信输入；真正的评测必须进容器，那是 E2-T2 / E4-T2 的事。

---

## 加一道新题

1. 抄一个现成的目录：`cp -r sources/bench-golden__auth-2 sources/bench-golden__<名字>-<编号>`
2. 改 `base/`（放有 bug 的版本和原有测试）和 `fix/`（放修好的源码和新测试）
3. 改 `issue.md`：第一行 `# 标题`，正文至少 200 字，**不要贴修复代码，不要放 PR 链接**
4. 改 `task.toml`：`task_id` 必须和目录名一致，列全 `fail_to_pass` 和 `pass_to_pass`
5. `make golden && make golden-verify`

写 issue 时按真实 issue 的样子来：现象、复现步骤、影响、期望行为。
描述"期望行为"时容易顺手把实现贴进去，那等于把答案发下去了 ——
`test_issue_does_not_contain_the_answer` 会拦这种情况。

`task_id` 的格式是 `{owner}__{repo}-{编号}`，和 SWE-bench 兼容，编号当作 PR 号用。
