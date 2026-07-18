# OpenHands Web Search 精确匹配 exact-v5

## 输入数据

当前实现针对：

```text
/home/liyachen/workspace/experiments/traces/
  deep_search_2026_7_12_15_29/tool-records/tool_calls.jsonl
```

该 trace 共 46 条记录：

- terminal 21 条，其中 curl 6 条；
- `browser_navigate` 12 条；
- `browser_get_content` 6 条，`browser_get_state` 3 条；
- think 及其他状态依赖 browser 操作 4 条。

当前实现只导入明确的 Web Search 工具和搜索 URL；普通 curl、普通 browser 导航和普通页面内容不进入 exact 索引。

搜索型 `browser_navigate` 仍然只能 `match_only`，因为其 observation 不包含页面结果且会改变 tab 状态。只有严格只读的搜索响应才允许自动复用。

## 匹配与复用是两个概念

查询结果分别返回：

- `matched`：数据库中存在相同 exact key；
- `reusable`：记录同时满足成功、新鲜、可安全重放；
- `reason`：不能重放时的具体原因。

失败、过期和有副作用的记录仍然存储，可以用于诊断，但不会被 OpenHands hook 自动复用。

## curl exact key

curl 的 canonical payload 包含：

```json
{
  "key_version": "exact-v5",
  "cache_scope": "local/default-tools/v1",
  "tool_name": "terminal",
  "action_kind": "TerminalAction",
  "operation_kind": "web_search_curl",
  "request": {
    "method": "GET",
    "url": "https://example.com/search?a=1&b=2",
    "headers": [],
    "secret_headers": [],
    "body_hash": null,
    "auth_scope": [],
    "options": {}
  },
  "output": {
    "postprocess": "grep semantic '|' head -50",
    "side_effects": []
  }
}
```

规范化规则：

- URL scheme/host 小写；
- 移除默认端口和 fragment；
- query 参数和重复参数保留原始顺序；
- `-s/-S` 可忽略；timeout、retry 等会改变终端 observation 的参数拒绝 exact；
- method、headers、body、认证 scope、`-L/-i/--compressed` 进入 exact key；
- pipe 后的 `grep/head` 等输出变换进入 exact key；
- `Authorization`、cookie、API key、URL userinfo 和敏感 query 会使 exact 调用不受支持；
- 入库的原始命令会脱敏。

以下搜索命令可以精确匹配：

```bash
curl -s --max-time 15 'https://example.com/search?b=2&a=1' | head -20
curl 'https://example.com/search?a=1&b=2' --max-time 99 -sS | head -20
```

以下命令不能直接重放：

```bash
curl URL -o file.pdf
curl URL > result.html
curl URL -D headers.txt
```

它们会得到 `matched=true`、`reusable=false`，因为阻断执行会导致目标文件没有创建。

## browser URL exact key

`browser_navigate` 使用以下字段匹配：

- canonical URL；
- `new_tab`；
- action kind；
- cache scope。

当前 trace 的 BrowserObservation 只包含 `Navigated to: URL`，没有网页正文，而且 navigate 会改变 tab 状态。因此 browser URL 可以精确匹配历史，但固定返回 `reusable=false`。

`browser_get_state` 依赖当前 tab/page 状态，仅凭 tool input 无法构造正确 exact key，因此不导入。

## SQLite 存储

数据库：

```text
/home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite
```

主表为 `exact_entries`，保存：

- exact key 和 canonical JSON；
- tool/action/operation 类型；
- 原始 OpenHands record key 和 trace 路径；
- 成功、失败、replayable、freshness、TTL；
- 脱敏后的 tool input；
- 完整 tool response 和 response hash；
- observation 时间及过期时间。

数据库打开时权限会校正为 `0600`。

## 使用

建库：

```bash
cd /home/liyachen/workspace/tool-reuse
python3 -m tool_reuse.cli exact-ingest \
  --records /home/liyachen/workspace/experiments/traces/deep_search_2026_7_12_15_29/tool-records \
  --db /home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite \
  --scope local/default-tools/v1
```

匹配：

```bash
python3 -m tool_reuse.cli exact-match \
  --db /home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite \
  --scope local/default-tools/v1 \
  --tool web_search \
  --input-json '{"kind":"SearchAction","query":"OpenHands tool reuse"}'
```

查看完整缓存响应时增加 `--full-response`。

统计：

```bash
python3 -m tool_reuse.cli exact-stats \
  --db /home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite
```

## OpenHands hook

`tool_reuse/hook_query.py` 已使用 exact-v5。只有同时设置 `TOOL_REUSE_DB` 与 `TOOL_REUSE_SCOPE` 且 `reusable=true` 才返回 deny、结构化 `toolResponse` 与 provenance；OpenHands SDK 会将其校验为 `execution_source=hook_replacement` 的 observation。普通 curl、browser 导航、失败记录、过期记录和有副作用搜索调用都返回 allow。
