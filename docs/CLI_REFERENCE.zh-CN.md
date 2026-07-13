# CLI 与返回字段

入口：

```bash
cd /home/liyachen/workspace/tool-reuse
python3 -m tool_reuse.cli --help
```

## exact-ingest

```bash
python3 -m tool_reuse.cli exact-ingest --records PATH --db DB
```

返回：

```json
{
  "seen": 47,
  "imported": 24,
  "unsupported": 23,
  "operation_counts": {
    "browser_navigate_url": 4,
    "curl_http": 20
  },
  "status_counts": {
    "failed": 2,
    "success": 22
  }
}
```

- `seen`：读取到的 JSONL 记录数；
- `imported`：exact-v2 支持并写入的记录数；
- `unsupported`：输入不完整或工具类型不支持；
- 失败 observation 也会导入，但不能复用。

## exact-match

```bash
python3 -m tool_reuse.cli exact-match \
  --db DB \
  --tool terminal \
  --input-json '{"kind":"TerminalAction","command":"curl https://example.com"}' \
  --limit 20 \
  --full-response
```

参数：

- `--limit`：最多读取多少条同 exact key 历史；
- `--full-response`：在 selected 中包含完整 input/response。

关键返回字段：

| 字段 | 含义 |
|---|---|
| `supported` | 当前 tool/action 是否有 exact normalizer |
| `matched` | 是否存在相同 exact key |
| `reusable` | 是否可以直接使用缓存 observation |
| `reason` | 当前决策原因 |
| `canonical` | 参与 hash 的规范化结构 |
| `query_policy` | TTL 和 replay policy |
| `selected` | matcher 选择的历史记录 |
| `matches` | 同 key 历史列表 |
| `tool_response` | 仅 reusable 且请求完整响应时返回 |

注意：`matched=true` 不等于 `reusable=true`。

## exact-stats

```bash
python3 -m tool_reuse.cli exact-stats --db DB
```

按 operation 输出总数、成功数和 replayable 数。

## semantic-ingest

```bash
python3 -m tool_reuse.cli semantic-ingest \
  --records PATH \
  --db DB \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5 \
  --batch-size 32
```

Embedding 参数：

```text
--provider sentence-transformers|openai-compatible|hashing
--model MODEL
--base-url URL
--api-key-env ENV_NAME
--dimensions N
--query-prefix TEXT
--document-prefix TEXT
--device DEVICE
```

返回中 `embedding_provider/embedding_model` 标识实际索引。

## semantic-match

```bash
python3 -m tool_reuse.cli semantic-match \
  --db DB \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5 \
  --tool terminal \
  --input-json JSON \
  --top-k 5 \
  --candidate-k 50 \
  --min-score 0.65 \
  --dense-weight 0.8 \
  --lexical-weight 0.2
```

可选：

```text
--include-stale
--full-response
--reranker-model MODEL
--rerank-top-n N
```

关键返回字段：

| 字段 | 含义 |
|---|---|
| `matched` | 至少一条候选达到 min score |
| `reusable` | semantic-v1 固定为 false |
| `semantic_text` | 当前输入生成的检索文本 |
| `candidate_count` | freshness/operation/model 过滤后的候选总数 |
| `dense_score` | cosine，相同方向越接近 1 |
| `lexical_score` | 当前候选集内归一化 BM25 |
| `metadata_boost` | host/method 结构化加分 |
| `hybrid_score` | dense + lexical + metadata |
| `rerank_score` | 启用 reranker 后的分数 |
| `final_score` | 用于阈值和排序的最终分数 |

`candidate_count` 不是最终返回数。最终 `candidates` 还会经过 `min_score` 和 `top_k`。

## semantic-stats

```bash
python3 -m tool_reuse.cli semantic-stats --db DB
```

按 provider、model、operation 输出记录数量和成功数量。
