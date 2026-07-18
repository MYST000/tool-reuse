# OpenHands 工具语义匹配 semantic-v3

## 架构

semantic-v3 采用：

```text
OpenHands tool input
  -> 结构化 semantic text
  -> Bi-Encoder embedding
  -> 同 operation/model 候选过滤
  -> cosine dense score
  -> BM25 lexical score
  -> metadata boost
  -> 可选 Cross-Encoder rerank
  -> 返回候选，由 equivalence judge 决定是否复用
```

语义命中固定返回 `reusable=false`。它只负责召回候选，不能像 exact-v5 一样直接阻断工具执行。

## 当前支持的工具

- 明确白名单中的 Web Search 工具：提取 `query/search_query/q/keywords/text`；
- 严格只读的 terminal curl：method、host、path、query 和只读 pipe 后处理构造语义文本；
- 带明确 URL 的 browser 历史记录：从 navigate、get state 或 observation 的 `<url>` 和 `<webpage_content>` 提取候选，页面正文最多索引 12,000 字符。

候选默认必须属于同一 `operation_kind`：

- `web_search_curl` 只匹配搜索型 curl 历史；
- `web_search_browser` 匹配搜索型 browser 及其页面正文历史；
- `web_fetch_curl` 匹配普通只读 Web 获取历史；
- `browser_page` 匹配普通 browser 页面历史；
- `web_search` 只匹配搜索历史。

这样不会把 curl observation 当作 browser observation 返回。

## Embedding provider

### Sentence Transformers / BGE

生产环境推荐。需要安装：

```bash
pip install sentence-transformers
```

建索引：

```bash
cd /home/liyachen/workspace/tool-reuse
python3 -m tool_reuse.cli semantic-ingest \
  --records /home/liyachen/workspace/experiments/traces/deep_search_2026_7_12_15_29/tool-records \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_cache.sqlite \
  --scope local/default-tools/v1 \
  --trust-legacy-origins \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5 \
  --query-prefix '为这个句子生成表示以用于检索相关文章：'
```

document 默认不加 prefix。建库和查询必须使用相同 model/config。

### OpenAI-compatible embeddings

可以连接 vLLM 或其他提供 `/v1/embeddings` 的服务：

```bash
export EMBEDDING_API_KEY=your-key

python3 -m tool_reuse.cli semantic-ingest \
  --records /path/to/tool-records \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_cache.sqlite \
  --scope local/default-tools/v1 \
  --provider openai-compatible \
  --base-url http://127.0.0.1:8000/v1 \
  --model BAAI/bge-m3
```

查询时使用相同 provider、base URL 和 model。

### Hashing provider

```bash
--provider hashing --dimensions 384
```

它无外部依赖，只用于测试 SQLite、pipeline 和排序是否工作，不具备生产语义质量。

## Hybrid score

默认：

```text
hybrid = 0.8 * cosine + 0.2 * normalized_BM25 + metadata_boost
```

metadata boost：

- URL 完全相同：`+0.70`；
- host 相同：`+0.03`；
- HTTP method 相同：`+0.01`。

URL 强加权用于抵消长网页正文对 document embedding 的稀释，使同一 URL 的 `browser_get_content` 候选能优先返回。它仍然只是候选排序，不会改变 `reusable=false`。

参数可以调整：

```bash
--dense-weight 0.8
--lexical-weight 0.2
--min-score 0.65
--candidate-k 50
--top-k 5
```

阈值依赖模型，需要用真实 tool-call 正负样本校准，不能把 `0.65` 当作所有模型的固定标准。

## Cross-Encoder rerank

安装 Sentence Transformers 后，可以增加：

```bash
--reranker-model BAAI/bge-reranker-v2-m3 --rerank-top-n 10
```

系统先用 embedding + BM25 召回，再对前 N 条逐对打分。reranker score 会成为这些候选的 final score。

## CLI

入库：

```bash
python3 -m tool_reuse.cli semantic-ingest \
  --records /path/to/tool-records \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_cache.sqlite \
  --scope local/default-tools/v1 \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5
```

查询：

```bash
python3 -m tool_reuse.cli semantic-match \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_cache.sqlite \
  --scope local/default-tools/v1 \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5 \
  --tool web_search \
  --input-json '{"kind":"SearchAction","query":"semantic retrieval embeddings"}'
```

默认只检索 fresh 成功记录。调试历史过期数据时使用 `--include-stale`。需要完整 response 时使用 `--full-response`。

统计：

```bash
python3 -m tool_reuse.cli semantic-stats \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_cache.sqlite \
  --scope local/default-tools/v1
```

## SQLite

表 `semantic_entries` 保存：

- provider、model、embedding dimension 和 float32 BLOB；
- semantic text、operation kind、结构化 metadata；
- success、TTL、过期时间；
- 脱敏后的 tool input；
- 脱敏后的 tool response、response preview/hash。

provider、model、dimensions、base URL、prefix 和 semantic version 会参与索引隔离。数据库权限每次打开时校正为 `0600`。

当前实现使用 SQLite 读取同 operation 的向量后计算 cosine，适合目前几十到几万条记录。数据达到十万级以上时，可以保持相同 schema/normalizer，把候选召回替换为 sqlite-vec、pgvector 或 Qdrant ANN。
