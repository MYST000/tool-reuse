# 安装与配置

## 基础环境

```bash
cd /home/liyachen/workspace/tool-reuse
python3 --version
sqlite3 --version
```

需要 Python 3.10+。exact-v5 和 hashing/OpenAI-compatible provider 只使用 Python 标准库。

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 数据路径

`--records` 可以是：

- 包含 `tool_calls.jsonl` 的 `tool-records` 目录；
- `tool_calls.jsonl` 文件本身。

推荐每种索引使用独立数据库：

```text
data/exact_cache.sqlite
data/semantic_bge.sqlite
data/semantic_remote.sqlite
```

不要把 exact、不同 embedding 模型或不同 provider 的索引混写到同一个数据库。

## Sentence Transformers

安装：

```bash
pip install sentence-transformers
```

默认 provider/model：

```text
provider: sentence-transformers
model: BAAI/bge-small-zh-v1.5
```

可选参数：

| 参数 | 说明 |
|---|---|
| `--model` | Hugging Face model ID 或本地模型目录 |
| `--device` | `cpu`、`cuda`、`cuda:0` 等 |
| `--query-prefix` | 仅查询向量前缀 |
| `--document-prefix` | 仅历史文档向量前缀 |
| `--batch-size` | ingest 批大小，默认 32 |

BGE 中文检索可按模型说明设置 query instruction，例如：

```bash
--query-prefix '为这个句子生成表示以用于检索相关文章：'
```

建库和查询应保持 provider、model、dimensions、base URL、query/document prefix 一致；这些配置会参与索引隔离。

## OpenAI-compatible Provider

服务必须提供：

```text
POST <base-url>/embeddings
```

如果服务地址是 `http://127.0.0.1:8000/v1/embeddings`，参数应为：

```bash
--base-url http://127.0.0.1:8000/v1
```

认证默认读取：

```bash
export EMBEDDING_API_KEY=your-key
```

自定义变量名：

```bash
--api-key-env MY_EMBEDDING_TOKEN
```

完整示例：

```bash
python3 -m tool_reuse.cli semantic-ingest \
  --records /path/to/tool-records \
  --db semantic_remote.sqlite \
  --scope local/default-tools/v1 \
  --provider openai-compatible \
  --base-url http://127.0.0.1:8000/v1 \
  --model BAAI/bge-m3 \
  --dimensions 1024
```

只有服务支持 `dimensions` 参数时才传 `--dimensions`。

## Hashing Provider

```bash
--provider hashing --dimensions 384
```

用途：

- 单元测试；
- 验证 JSONL 导入；
- 验证 SQLite 和 hybrid 排序；
- 无模型时演示 CLI。

它不是神经 embedding，不能用于生产语义质量评估。

## Reranker

需要 `sentence-transformers`：

```bash
--reranker-model BAAI/bge-reranker-v2-m3
--rerank-top-n 10
```

reranker 只在查询时运行，不需要重建 embedding 索引。

## 匹配参数

| 参数 | 默认 | 说明 |
|---|---:|---|
| `--top-k` | 5 | 最多返回多少条通过阈值的候选 |
| `--candidate-k` | 50 | hybrid 初排保留数量 |
| `--min-score` | 0.65 | final score 阈值 |
| `--dense-weight` | 0.8 | cosine 权重 |
| `--lexical-weight` | 0.2 | BM25 权重 |
| `--include-stale` | false | 是否包含过期记录 |
| `--full-response` | false | 是否返回完整 tool response |

阈值需要按模型校准，详见 [EVALUATION.zh-CN.md](EVALUATION.zh-CN.md)。

## TTL 配置

TTL 当前是代码策略，不是 CLI 参数：

```text
tool_reuse/exact/policy.py
```

修改后需要重新 ingest，数据库中的 `expires_at_epoch` 才会更新。

## 数据安全

- 数据库权限自动设置为 `0600`；
- tool input 中常见认证信息会脱敏；
- 认证调用不进入 exact，检测到 credential 的 response 也拒绝持久化；
- semantic tool input、response、metadata 和 embedding text 在落盘/远程请求前脱敏；
- exact 与 semantic 都要求 scope 包含用户/租户、provider、工具和配置版本；
- 不要把 SQLite 文件提交到公共仓库；
- 多用户环境应为每个用户或认证域使用独立数据库。
