# 架构与数据流

## 目标

系统解决两个不同问题：

1. 当前工具调用是否和某次历史调用完全等价；
2. 如果不完全等价，历史中是否存在语义上相关的结果可供 agent 参考。

两者不能共享同一命中策略。精确匹配可以在严格条件下自动复用；语义匹配只能召回候选。

## 组件

```text
tool_calls.jsonl
  |
  +-> exact.ingest
  |     -> exact.normalize
  |     -> exact_entries (SQLite)
  |
  +-> semantic.ingest
        -> semantic.normalize
        -> embedding provider
        -> semantic_entries (SQLite)

incoming tool call
  |
  +-> exact.matcher -> reusable ? cached response : continue
  |
  +-> semantic.matcher -> top-k candidates
                            |
                            +-> optional Cross-Encoder
                            +-> equivalence judge / agent
```

## OpenHands 输入

完成记录位于 `tool_calls.jsonl`，典型结构：

```json
{
  "record_key": "...",
  "tool_name": "web_search",
  "started_at": "2026-07-08T08:08:54+00:00",
  "ended_at": "2026-07-08T08:08:55+00:00",
  "tool_input": {
    "kind": "SearchAction",
    "query": "OpenHands SDK documentation",
    "domains": ["docs.openhands.dev"]
  },
  "tool_response": {
    "kind": "TerminalObservation",
    "is_error": false,
    "exit_code": 0,
    "content": [{"type": "text", "text": "..."}]
  }
}
```

`pending/` 中没有 response 的调用不参与导入。

## Exact-v3

入口：`tool_reuse/exact/`。

### 支持范围

- 明确白名单中的 Web Search 工具（`web_search`、`browser_search`、`search`）；
- URL 明确为搜索端点的 terminal curl（仅严格只读调用可重放）；
- URL 明确为搜索端点的 `browser_navigate`（只能 `match_only`）。

不支持：

- `browser_get_state`：输入没有当前 page/tab identity；
- file editor：属于状态修改；
- think/finish：没有可复用的外部结果；
- 普通 HTTP、普通页面导航和普通 shell 命令：不进入 Web Search 缓存。

### Canonical key

curl key 由以下部分计算 SHA-256：

- key version、tool name、action kind；
- HTTP method 和 canonical URL；
- 会影响响应的 headers、body hash、认证 scope；
- redirect/include/compressed 等响应相关选项；
- pipe 后处理；
- 文件副作用描述。

不会进入 key：

- `-s/-S`；
- timeout；
- retry 参数；
- 进度显示和 fail-fast 参数。

browser key 包含 canonical URL、`new_tab` 和 action kind。

### 记录选择

同一 exact key 可能对应多次历史调用。matcher 按以下优先级选择：

1. 最新的成功、fresh、replayable 记录；
2. 最新成功记录；
3. 最新失败记录。

只有第一类得到 `reusable=true`。

### Replay policy

```text
response    可以把 observation 作为缓存结果注入 agent
match_only  只能报告历史匹配，不能阻断真实工具调用
```

以下情况是 `match_only`：

- shell redirect；
- `curl -o/-O/-D`；
- cookie jar 写入；
- browser navigation。

## Semantic-v1

入口：`tool_reuse/semantic/`。

### Semantic text

embedding 不直接编码完整原始 JSON。curl、navigate 和 search 编码规范化后的调用意图；`browser_get_content` 历史记录会从 response 中提取 URL 与最多 12,000 字符网页正文，作为可检索页面文档。

curl 示例：

```text
web fetch http get www.sbert.net examples semantic search readme html grep semantic head 50
```

browser 示例：

```text
browser navigate web page www.sbert.net examples semantic search readme html
```

search 示例：

```text
web search query 中文 embedding 模型
```

除 `browser_get_content` 的页面正文外，response 只作为候选载荷保存。页面正文候选仍归入 `web_search_browser`，因此搜索 URL 查询既能找到导航历史，也能找到实际页面内容。

### 候选隔离

检索必须同时满足：

- embedding provider 相同；
- embedding model 相同；
- operation kind 相同；
- 默认只使用成功且 fresh 的记录。

operation kind 包括：

```text
web_search_curl
web_search_browser
web_search
```

### Hybrid score

无 reranker 时：

```text
hybrid =
  dense_weight * max(0, cosine)
  + lexical_weight * normalized_BM25
  + metadata_boost
```

默认权重在归一化后为 `0.8/0.2`。URL 完全一致增加 `0.70`，同 host 增加 `0.03`，同 HTTP method 增加 `0.01`，最终截断到 1。

启用 Cross-Encoder 后，前 `rerank_top_n` 条候选以 reranker score 作为 final score，其余候选保留 hybrid score。

semantic matcher 固定返回 `reusable=false`。

## Freshness

URL TTL 在 `tool_reuse/exact/policy.py` 中定义，exact 和 semantic URL 记录共用：

| 分类 | TTL | 示例 |
|---|---:|---|
| volatile | 5 分钟 | latest、today、price、weather |
| search | 10 分钟 | `/search`、`?q=` |
| immutable | 30 天 | commit SHA、arXiv abs/pdf |
| static | 7 天 | docs、README、raw GitHub、PDF |
| web | 6 小时 | 其他普通 URL |

`web_search` 固定为 10 分钟。

TTL 是自动复用策略，不影响历史记录是否保存在数据库。exact matcher 会报告 stale match；semantic matcher 默认过滤 stale，可用 `--include-stale` 查看。

## SQLite

### exact_entries

关键列：

- `record_key`：OpenHands 历史记录 ID；
- `exact_key`、`key_version`、`canonical_json`；
- `operation_kind`；
- `success`、`replayable`、`replay_policy`；
- `observed_at_epoch`、`expires_at_epoch`；
- `tool_input_json`、`tool_response_json`；
- `response_text`、`response_sha256`。

### semantic_entries

关键列：

- `(record_key, embedding_provider, embedding_model)` 联合主键；
- `embedding_dim` 和 float32 `embedding_blob`；
- `semantic_text`、`metadata_json`；
- freshness/status 字段；
- 脱敏输入和完整 response。

## 并发与一致性

- 导入使用 SQLite upsert，相同 record/model 可重复执行；
- 当前没有后台 watcher，新增 JSONL 记录后需要重新执行 ingest；
- matcher 每次打开独立 SQLite connection；
- 当前未启用 WAL，也没有长期写事务，适合单机实验和低并发 hook；
- 多 agent 高频并发写入时应增加 WAL、busy timeout 和独立索引服务。

## 扩展边界

当前 semantic matcher 会在同 operation/model 记录中线性计算 cosine，适合几十到几万条记录。规模变大时可以保留 normalizer 和记录 schema，仅替换召回层：

- sqlite-vec：本地单文件；
- pgvector：已有 PostgreSQL 环境；
- Qdrant/Milvus：独立向量服务；
- FAISS：只需要本地 ANN，不需要数据库过滤。
