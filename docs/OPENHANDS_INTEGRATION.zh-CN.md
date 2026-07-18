# OpenHands 集成

本文说明如何把 `tool-reuse` 的 Web Search 精确匹配接入 OpenHands 的 `PreToolUse` hook。语义检索只用于候选召回和评估，不应在 hook 中自动阻止工具执行。

## 前置条件

在项目目录初始化精确索引：

```bash
cd /home/liyachen/workspace/tool-reuse
python3 -m tool_reuse.cli exact-ingest \
  --records /home/liyachen/workspace/experiments/traces/deep_search_2026_7_12_15_29/tool-records \
  --db data/exact_cache.sqlite \
  --scope local/default-tools/v1
```

首次接入前先手工查询一条已知记录，确认规范化、TTL 和响应内容符合预期：

```bash
python3 -m tool_reuse.cli exact-match \
  --db data/exact_cache.sqlite \
  --scope local/default-tools/v1 \
  --tool web_search \
  --input-json '{"kind":"SearchAction","query":"OpenHands tool reuse"}'
```

## hooks.json

把下面的 `PreToolUse` 项合并到 OpenHands 已有的 hook 配置中，不要覆盖其他 hook。仓库中的 [示例](../examples/hooks.pre_tool_use.json) 可作为起点。

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "terminal|browser",
        "hooks": [
          {
            "type": "command",
            "command": "TOOL_REUSE_DB=/home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite TOOL_REUSE_SCOPE=local/default-tools/v1 PYTHONPATH=/home/liyachen/workspace/tool-reuse python3 /home/liyachen/workspace/tool-reuse/tool_reuse/hook_query.py"
          }
        ]
      }
    ]
  }
}
```

OpenHands 版本之间的配置外层结构可能不同；保留当前版本生成的结构，只合并 `PreToolUse` 条目。命令必须使用绝对数据库路径，避免 OpenHands 工作目录变化后读到另一个数据库。

## Hook 顺序

建议按以下顺序执行：

1. 权限、安全策略和命令审计。
2. `tool-reuse` 精确缓存查询。
3. 预算、限流等会阻止真实调用的策略。
4. 真实工具调用。

安全策略必须先于缓存查询。缓存命中不能绕过当前会话的网络、域名、文件或权限约束。

## 输入事件

`tool_reuse.hook_query` 从标准输入读取一个 JSON 事件。核心字段是工具名与工具输入；具体字段包装由 OpenHands hook 版本决定。接入时至少保留：

```json
{
  "tool_name": "web_search",
  "tool_input": {
    "kind": "SearchAction",
    "query": "OpenHands API documentation",
    "domains": ["docs.openhands.dev"]
  }
}
```

不要在命令行参数中拼接未经转义的工具输入。标准输入 JSON 能保留引号、换行和结构化 browser 参数。

## allow 与 deny

- 未命中、记录过期、原调用失败或结果不可重放时：返回 `allow`，让工具正常执行。
- 命中且 `reusable=true` 时：返回 `deny` 和 `toolResponse`；需要使用包含替代 observation 支持的 OpenHands SDK 版本。
- hook 自身解析失败或数据库不可用时：应 fail open，即 `allow`，同时写诊断日志。

只有 `exact-v5` 同时满足显式成功、未过期、可重放且 scope 一致时才允许自动复用。普通 curl/browser 调用不会进入该路径；语义相似不等于调用等价，因此 `semantic-v3` 固定返回 `reusable=false`。

## 索引更新

hook 查询不会自动把新 trace 写入索引。采集任务完成后应批量重建或增量导入：

```bash
python3 -m tool_reuse.cli exact-ingest \
  --records /path/to/new/tool-records \
  --db data/exact_cache.sqlite \
  --scope local/default-tools/v1
```

建议使用临时数据库完成构建和完整性检查，再原子替换线上数据库，避免 hook 读到半成品。多进程并发读写时启用 SQLite WAL，并为写入设置合理的 busy timeout。

## 副作用边界

默认只自动复用明确的 Web Search 读取操作。以下操作不进入自动复用：

- POST、PUT、PATCH、DELETE 等写请求。
- 包含上传、认证交换、支付或一次性 token 的请求。
- 普通 curl、普通 browser 导航、shell 中包含重定向、未知管道命令或其他有副作用命令的调用。
- 依赖 cookie、当前登录态、工作目录或本地文件内容的调用。

缓存响应也可能包含密钥、cookie 或个人数据。数据库权限应限制到运行 agent 的账户，日志中不得输出完整认证头和响应正文。
