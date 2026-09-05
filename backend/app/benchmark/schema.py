"""题目 Schema（`docs/plan/03-benchmark-spec.md` §7.1，**冻结件**）。

一道题就是这么几样东西：代码快照（`repo_name` + `base_commit`）、给被测 AI 看的
issue 描述、一组"修好之后必须由失败变通过"的测试（`fail_to_pass`）、
一组"不能被改坏"的测试（`pass_to_pass`），外加官方补丁（`gold_patch`）做参考解。

字段定义抄自 §7.1，**不要在这里加减字段**。要改先走 §7.1 的变更流程。

## 这个模型做两件事

1. **解析和序列化**：JSON ↔ `TaskDefinition`，双向无损。
2. **拒收坏题**：坏题比没题更糟——它会让解决率无声地偏掉，而且极难查。
   所有校验规则都写成"命中就抛错，错误消息里说清楚是哪条规则、实际值是什么"。

硬性拒收（构造时抛 `ValidationError`）和需要人工看一眼（`review_flags()`）
是分开的：前者是数据本身不合法，后者是数据合法但可疑（§7.4 的 `REVIEW_REQUIRED`）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.benchmark.hashing import compute_content_hash
from app.domain.enums import IssueLanguage, TaskDifficulty, TaskValidationState
from app.domain.execution_plan import ExecutionPlan
from app.domain.patch_paths import derive_patch_paths
from app.domain.protected_paths import (
    DEFAULT_PROTECTED_PATTERNS,
    agent_visible_patterns,
    is_protected,
    protected_hits,
)
from app.runner.protocol import (
    AgentTaskInput,
    Constraints,
    IssueInput,
    ModelInput,
    RepoInput,
    assert_no_leak,
)

#: `{owner}__{repo}-{pr_number}`，与 SWE-bench 的命名兼容。
#: 仓库名里可以有 `-`，所以 PR 号匹配的是**最后**一段 `-数字`。
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][\w.-]*__[A-Za-z0-9][\w.-]*-\d+$")

#: 40 位小写全 SHA。§7.2(2)：禁止短 SHA、分支名、tag。
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

#: issue 正文里出现这些就算泄题：指向 PR 的链接、或者干脆贴了一段 diff。
_LEAK_PR_URL = re.compile(r"https?://[^\s]*/(?:pull|merge_requests)/\d+", re.IGNORECASE)
_LEAK_DIFF_BLOCK = re.compile(r"^diff --git ", re.MULTILINE)

#: issue 短于这个长度就要人工看一眼（§7.2(7)）。不是硬性拒收——
#: 有些 issue 确实短，但配了清晰的复现步骤。
MIN_ISSUE_BODY_CHARS = 200

#: F2P 数量落在这个区间外要人工复核（§7.4）。
REVIEW_F2P_MAX = 20


class P2PSampling(BaseModel):
    """P2P 用例是怎么选出来的（§7.7）。

    全量套件跑一遍很贵（可能几千条用例），所以 P2P 不一定是"全部通过的用例"。
    抽样规则必须记下来，因为**抽样参数决定 P2P 名单，而 P2P 名单直接决定判定结论**：
    同一道题换一个随机种子，选中的回归护栏就不同，同一个补丁可能一次判过一次判挂。
    所以这一段纳入 `content_hash`，不记的话"同一个数据集版本"给不出同样的判定。
    """

    model_config = ConfigDict(extra="forbid")

    #: full = 全量通过用例都当 P2P（套件 ≤ 3 分钟时）；
    #: module_and_random = 同模块用例 ∪ 固定种子随机抽样。
    strategy: Literal["full", "module_and_random"]
    #: 随机抽样用的种子。`full` 策略没有随机性，必须为 null。
    seed: int | None = None
    #: 候选池大小 —— 抽样之前一共有多少条通过的用例。
    total_pool: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_seed_matches_strategy(self) -> Self:
        """种子和策略要对得上。

        `module_and_random` 缺种子是个真问题：没有种子就复现不出当初选了哪 200 条，
        题目也就不可复现了 —— 而这正是记录这一段的全部意义。
        """
        if self.strategy == "module_and_random" and self.seed is None:
            raise ValueError(
                "strategy=module_and_random 必须给 seed，否则抽出来的 P2P 名单无法复现（§7.7）"
            )
        if self.strategy == "full" and self.seed is not None:
            raise ValueError(f"strategy=full 没有随机性，不该有 seed：{self.seed}")
        return self


class TaskValidation(BaseModel):
    """题目验证的结论与证据（§7.3 八步流水线跑完写进来）。

    这一段**不参与** `content_hash`：它记的是验证过程的结果，不是题目内容。
    每周复验会更新 `validated_at`，但题目本身没变。
    """

    model_config = ConfigDict(extra="forbid")

    state: TaskValidationState
    validated_at: datetime | None = None
    validator_version: str | None = None
    #: 验证时实际用的镜像 digest。协议 C-36：引用镜像用 digest 不用 tag，tag 会被覆盖。
    image_digest: str | None = None
    evidence_artifact_uri: str | None = None


class TaskDefinition(BaseModel):
    """一道评测题的完整定义（§7.1）。"""

    # extra="forbid"：JSON 里多一个字段就报错。宽容处理的代价是字段拼错了不报错，
    # 那个值静默地不生效——比如 `fail_to_pass` 写成 `failed_to_pass`，
    # 题目会变成"没有 F2P"，而这本该是拒收条件。
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    dataset_id: str

    # ── 仓库与快照 ──
    repo_url: str
    repo_name: str
    #: 代码回退到的提交，也就是"bug 还在"的状态。必须是修复 PR 的第一父提交。
    base_commit: str
    #: 指向 `environment_specs`，决定用哪个镜像。
    #: 任务不直接记 `docker_image`——镜像是环境的属性，多个任务共享同一个环境。
    environment_id: str

    # ── 问题描述（给被测 AI 的唯一输入）──
    issue_title: str
    issue_body: str
    issue_language: IssueLanguage
    #: 默认 null，即不给提示，对齐 SWE-bench Verified。
    hints_text: str | None = None

    # ── 执行定义（继承自环境规格，可覆盖）──
    install_command: str
    pre_test_command: str | None = None
    test_command: str
    test_framework: Literal["pytest", "unittest", "jest", "gotest", "junit"] = "pytest"
    test_report_path: str

    # ── 验证测试 ──
    #: 仅含测试文件的 diff，由 harness 施加，不下发给被测 AI。
    test_patch: str
    #: `test_patch` 实际改动的全部路径，由本模型从 `test_patch` 重算并校验（C-74）。
    #: **禁止下发给被测 AI**（C-76）。
    test_patch_paths: list[str] = Field(default_factory=list)
    fail_to_pass: list[str]
    pass_to_pass: list[str] = Field(default_factory=list)
    #: P2P 是怎么抽出来的（§7.7）。全量套件小的时候可以不抽样，所以允许为 null。
    p2p_sampling: P2PSampling | None = None

    # ── 参考解 ──
    #: 仅含非测试文件的 diff，**永不下发给被测 AI**（协议 C-44）。
    gold_patch: str

    # ── 预算 ──
    agent_timeout_s: int = Field(default=720, ge=1)
    test_timeout_s: int = Field(default=480, ge=1)
    sandbox_cpu: float = Field(default=1.0, gt=0)
    sandbox_memory_mb: int = Field(default=1536, ge=256)
    sandbox_pids_limit: int = Field(default=512, ge=1)

    # ── 溯源与元数据 ──
    source_issue_url: str | None = None
    source_pr_url: str | None = None
    created_at_upstream: datetime | None = None
    language: str = "python"
    framework: str | None = None
    difficulty: TaskDifficulty
    tags: list[str] = Field(default_factory=list)

    # ── 完整性 ──
    #: `sha256:` 开头。缺省时自动算出来；给了就校验，对不上直接拒收。
    content_hash: str | None = None

    def execution_plan(self, *, extra_protected_paths: tuple[str, ...] = ()) -> ExecutionPlan:
        """导出测试执行器要的那部分（E4-T2）。

        为什么要转一道手：`app.evaluation` 和 `app.benchmark` 在模块边界里是并排的，
        并排就是互不可见，执行器 import 不到 `TaskDefinition`。转换只有这一处，
        两边各写一份的话迟早会漂。

        **刻意不带 `gold_patch` 和 `issue_body`**：跑一轮测试用不着官方答案，
        传进去只是多一条泄漏路径。

        `extra_protected_paths` 来自环境规格（`environment_specs.extra_protected_paths`），
        题目本身不带这个字段，由调用方从环境里取来传进来。
        """
        return ExecutionPlan(
            base_commit=self.base_commit,
            test_patch=self.test_patch,
            test_patch_paths=tuple(self.test_patch_paths),
            fail_to_pass=tuple(self.fail_to_pass),
            pass_to_pass=tuple(self.pass_to_pass),
            test_command=self.test_command,
            test_report_path=self.test_report_path,
            pre_test_command=self.pre_test_command,
            extra_protected_paths=extra_protected_paths,
            test_timeout_s=self.test_timeout_s,
            sandbox_cpu=self.sandbox_cpu,
            sandbox_memory_mb=self.sandbox_memory_mb,
            sandbox_pids_limit=self.sandbox_pids_limit,
            task_id=self.task_id,
        )

    def agent_task_input(
        self,
        *,
        deadline_unix_ms: int,
        model: str = "none",
        temperature: float = 0.0,
        max_tokens_budget: int | None = None,
        allow_network: bool = True,
        workspace_path: str = "/workspace",
        extra: dict[str, Any] | None = None,
    ) -> AgentTaskInput:
        """导出下发给被测 AI 的任务输入（E4-T4，Runner 协议 §9.2）。

        **这个对象里的每一个字段都会被被测 AI 看到**，所以防泄题的规矩全在这一处，
        别处不许自己拼一个：

        - `protected_paths` 用 `agent_visible_patterns()`，**不是**
          `enforcement_patterns()` —— 后者含该题的 `test_patch_paths`，
          下发出去等于告诉 AI 官方改了哪几个文件来验证（协议 C-76）。
        - `gold_patch`（官方答案）、`test_patch`、`fail_to_pass`、`pass_to_pass`、
          `test_command` 一个都不进去。
        - `repo` 只给名字和 base commit，**不给 URL**：给了 URL，AI 一句
          `git clone` 就能拉到官方修复。
        - 组装完再过一遍 `assert_no_leak()` 兜底 —— 前面几条靠"记得别写"，
          这一条靠机器检查。

        `deadline_unix_ms` 用绝对时刻不用"还剩几秒"：适配器可能几秒后才真正开始
        干活，传相对值的话这段启动时间就被白送给了 AI，两次运行的预算不一样。
        """
        task_input = AgentTaskInput(
            task_id=self.task_id,
            workspace_path=workspace_path,
            issue=IssueInput(
                title=self.issue_title, body=self.issue_body, language=self.issue_language
            ),
            repo=RepoInput(name=self.repo_name, base_commit=self.base_commit),
            hints=self.hints_text,
            constraints=Constraints(
                deadline_unix_ms=deadline_unix_ms,
                max_tokens_budget=max_tokens_budget,
                protected_paths=list(agent_visible_patterns()),
                allow_network=allow_network,
                allow_run_tests=True,
            ),
            model=ModelInput(name=model, temperature=temperature),
            extra=extra or {},
        )
        assert_no_leak(task_input.model_dump(mode="json"))
        return task_input

    validation: TaskValidation | None = None

    # ── 校验与规范化 ────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_and_normalize(self) -> Self:
        """按 §7 和协议的规则挨条查一遍，顺手把集合类字段规范化。

        顺序有讲究：先规范化，再校验，最后才算哈希——哈希必须建立在
        规范化之后的数据上，否则同样的内容换个写法会得到不同的哈希。
        """
        self._normalize_collections()
        self._check_identifiers()
        self._check_test_selection()
        self._check_patches()
        self._check_issue_not_leaking()
        self._settle_content_hash()
        return self

    def _normalize_collections(self) -> None:
        """集合语义的列表一律排序去重。

        这是 `content_hash` 对字段序不敏感的另一半：字段顺序由规范 JSON 的键排序
        解决，列表内部的顺序在这里解决。两道题只有 F2P 排列顺序不同的话，
        它们本来就是同一道题。
        """
        self.fail_to_pass = sorted(set(self.fail_to_pass))
        self.pass_to_pass = sorted(set(self.pass_to_pass))
        self.tags = sorted(set(self.tags))

    def _check_identifiers(self) -> None:
        if not TASK_ID_PATTERN.match(self.task_id):
            raise ValueError(
                f"task_id 不符合 {{owner}}__{{repo}}-{{pr_number}} 格式：{self.task_id!r}"
            )
        if not FULL_SHA_PATTERN.match(self.base_commit):
            raise ValueError(
                f"base_commit 必须是 40 位小写全 SHA，禁止短 SHA / 分支名 / tag（§7.2）："
                f"{self.base_commit!r}"
            )

    def _check_test_selection(self) -> None:
        if not self.fail_to_pass:
            raise ValueError(
                "fail_to_pass 不能为空：没有'修好才会通过'的测试，这道题无法判定（§7.2）"
            )
        overlap = sorted(set(self.fail_to_pass) & set(self.pass_to_pass))
        if overlap:
            raise ValueError(
                f"同一个用例不能既在 fail_to_pass 又在 pass_to_pass 里："
                f"{overlap[:5]}（共 {len(overlap)} 条）"
            )
        self._check_p2p_sampling()

    def _check_p2p_sampling(self) -> None:
        """抽样记录要和实际的 P2P 名单对得上（§7.7）。

        对不上说明这份记录是手写的或者过期的 —— 而它是"这批 P2P 怎么来的"的唯一凭据，
        错了的话没人能复现当初的抽样。
        """
        sampling = self.p2p_sampling
        if sampling is None:
            return
        selected = len(self.pass_to_pass)
        if selected > sampling.total_pool:
            raise ValueError(
                f"pass_to_pass 有 {selected} 条，比候选池 total_pool={sampling.total_pool} 还多，"
                f"抽样记录对不上（§7.7）"
            )
        if sampling.strategy == "full" and selected != sampling.total_pool:
            raise ValueError(
                f"strategy=full 表示候选池里的用例全都是 P2P，但 pass_to_pass 只有 {selected} 条，"
                f"候选池有 {sampling.total_pool} 条（§7.7）"
            )

    def _check_patches(self) -> None:
        """三条补丁规则，每条都对应一种已知的坏题或作弊路径。"""
        derived = derive_patch_paths(self.test_patch)
        if not derived:
            raise ValueError("test_patch 解析不出任何被改动的文件，不是合法的 unified diff")

        # C-74 第 6 条：重算一遍和已存清单比对。有人手工改这份清单想放开某个文件的
        # 保护时，这里会对不上。给了才比，没给就直接用算出来的。
        if self.test_patch_paths and sorted(set(self.test_patch_paths)) != derived:
            raise ValueError(
                f"test_patch_paths 与 test_patch 重新解析的结果不一致（协议 C-74 第 6 条）。"
                f"声明的={sorted(set(self.test_patch_paths))}，实际解析={derived}"
            )
        self.test_patch_paths = derived

        # §7.1：test_patch 只含测试文件。碰了业务代码的话，官方测试补丁就把 bug 一起
        # 修掉了，F2P 在 base 上也能过，这道题就没有区分度了。
        non_test = [p for p in derived if not is_protected(p, DEFAULT_PROTECTED_PATTERNS)]
        if non_test:
            raise ValueError(f"test_patch 只能改测试文件，但它改了：{non_test}")

        gold_paths = derive_patch_paths(self.gold_patch)
        if not gold_paths:
            raise ValueError("gold_patch 为空：这道题只改测试就能通过，没有修复内容（§7.2）")

        # 协议 C-64：gold_patch 命中受保护路径 → 题目无效。
        # 注意这里要连 test_patch_paths 一起算，不能只用通用规则。
        hits = protected_hits(tuple(gold_paths), (*DEFAULT_PROTECTED_PATTERNS, *derived))
        if hits:
            raise ValueError(f"gold_patch 命中了受保护路径（协议 C-64）：{hits}")

    def _check_issue_not_leaking(self) -> None:
        """issue 正文里不能带着答案（§7.2(7)）。

        只查两种没有歧义的形式：指向 PR 的链接、以及直接贴出来的 diff。
        不查裸的 commit hash——用户贴报错日志时带上哈希是很正常的，
        按那个拒收会误伤一大批好题。
        """
        if _LEAK_PR_URL.search(self.issue_body):
            raise ValueError("issue_body 里有指向 PR 的链接，等于把答案给了被测 AI（§7.2(7)）")
        if _LEAK_DIFF_BLOCK.search(self.issue_body):
            raise ValueError("issue_body 里贴了 diff 代码块，等于把答案给了被测 AI（§7.2(7)）")

    def _settle_content_hash(self) -> None:
        """算哈希；已经带了就核对。"""
        computed = compute_content_hash(self._hash_payload())
        if self.content_hash is not None and self.content_hash != computed:
            raise ValueError(
                f"content_hash 对不上，题目内容被改过或哈希算错了。"
                f"声明的={self.content_hash}，重算={computed}"
            )
        self.content_hash = computed

    def _hash_payload(self) -> dict[str, Any]:
        """喂给哈希函数的字典。排除项由 `hashing.EXCLUDED_FIELDS` 负责。"""
        return self.model_dump(mode="json")

    # ── 对外 ────────────────────────────────────────────────

    def review_flags(self) -> list[str]:
        """数据合法但可疑的地方，用来把题目路由到 `REVIEW_REQUIRED`（§7.4）。

        和构造时的硬性拒收分开：那些是数据不合法，这些是"合法但值得人看一眼"。
        混在一起的话，要么好题被拒，要么坏题混进数据集。
        """
        flags = []
        if len(self.issue_body) < MIN_ISSUE_BODY_CHARS:
            flags.append(
                f"issue_body 只有 {len(self.issue_body)} 字，少于 {MIN_ISSUE_BODY_CHARS}，"
                f"可能信息不足以让 AI 定位问题"
            )
        if len(self.fail_to_pass) > REVIEW_F2P_MAX:
            flags.append(
                f"fail_to_pass 有 {len(self.fail_to_pass)} 条，超过 {REVIEW_F2P_MAX}，"
                f"这道题可能一次改了太多东西"
            )
        if not self.pass_to_pass:
            flags.append("pass_to_pass 为空：没有回归护栏，AI 删掉功能也能通过 F2P（§7.2(6)）")
        return flags

    def agent_visible_dump(self) -> dict[str, Any]:
        """下发给被测 AI 的那份数据里，题目侧允许出现的字段。

        **不含** `gold_patch`（C-44）、`test_patch`、`test_patch_paths`（C-76）、
        `fail_to_pass`、`pass_to_pass`——用例 ID 也是定位提示。

        完整的下发格式是 Runner 协议的事（`04-runner-protocol.md`，E3-T1），
        这里只负责"题目这边哪些字段可以给"，把这条边界钉在题目模型上，
        免得每个适配器各自决定一次，漏一个就泄题。
        """
        return self.model_dump(
            mode="json",
            include={
                "task_id",
                "repo_name",
                "base_commit",
                "issue_title",
                "issue_body",
                "issue_language",
                "hints_text",
                "language",
                "framework",
                "test_command",
                "test_framework",
                "agent_timeout_s",
            },
        )


__all__ = [
    "FULL_SHA_PATTERN",
    "MIN_ISSUE_BODY_CHARS",
    "TASK_ID_PATTERN",
    "P2PSampling",
    "TaskDefinition",
    "TaskValidation",
]
