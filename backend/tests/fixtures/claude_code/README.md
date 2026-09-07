# 录下来的 stream-json（E3-T5）

`deepseek_gateway_edit.jsonl` 是 **2026-09-06 真跑出来的**，不是手写的：
`claude-code 2.1.236` 在容器里改一个只有三行的仓库（`add()` 写成了减法），
底座模型走 DeepSeek 的 Anthropic 兼容端点。

## 为什么要存一份真的

手写的样本只包含"我以为它会输出什么"，而漏掉的怪癖恰恰是出静默 bug 的地方。
这一份就推翻了两个我原本以为对的假设：

1. **一次 API 调用会发出多条 `assistant` 事件**（thinking 一条、tool_use 一条），
   而且**每条都带着同一份 usage**。照事件求和会把输入 token 翻倍
   （实测 40038 vs 真实 20019）。要按 `message.id` 去重。
2. **每条消息里的 `output_tokens` 都是 0**，真实的 181 只出现在 `result` 事件里。
   所以 `result.usage` 才是权威，逐条求和只能当兜底。

顺带确认了：CLI 版本在 `claude_code_version` 字段（不是 `version`），
工具结果成功时 `is_error` 是缺席而不是 `false`，
`system`/`thinking_tokens` 是一串进度心跳（这一份里有 37 条），不是用量。

## 重新录一份

跑 `tests/contract/test_claude_code_runner.py`（要 Key、花钱），或者手工：

    docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/workspace" -w /workspace \
      --tmpfs /tmp:rw,nosuid,size=512m,mode=1777 \
      -e ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic \
      -e ANTHROPIC_AUTH_TOKEN="$DEEPSEEK_API_KEY" \
      bench-agent:py311-claude-code \
      claude --print --output-format stream-json --verbose \
        --permission-mode bypassPermissions --max-turns 10 --model deepseek-chat '<题面>'

录之前确认里面没有 Key（`apiKeySource` 只会写变量名，不写值）。
