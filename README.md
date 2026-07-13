# OpenHands Tool Reuse

这是一个面向 OpenHands 工具历史的本地缓存与检索实现，读取：

```text
<trace>/tool-records/tool_calls.jsonl
```

项目提供两条互补路径：

- `exact-v2`：对 curl HTTP 请求和 browser URL 做规范化精确匹配；
- `semantic-v1`：使用 embedding + BM25 hybrid retrieval 返回语义候选。

推荐调用顺序：

```text
新工具调用
  -> exact-v2
     -> fresh + success + replayable：返回缓存结果
     -> miss / stale / unsafe：继续
  -> semantic-v1
     -> 返回 top-k 候选
  -> equivalence judge / agent 判断
     -> 复用候选或真实执行工具
```

语义匹配不会自动复用结果。只有 exact-v2 的安全命中会被 OpenHands pre-tool hook 阻断。

## 当前数据

已针对以下 trace 完成验证：

```text
/home/liyachen/workspace/experiments/traces/
  deep_search_2026_7_12_15_29/tool-records/tool_calls.jsonl
```

导入结果：

| 类型 | 记录数 | 成功 | exact 可重放 |
|---|---:|---:|---:|
| terminal curl | 6 | 6 | 4 |
| browser navigate | 12 | 12 | 0 |
| browser page content（仅 semantic） | 5 | 5 | 0 |
| 其他 terminal/browser/think | 23 | - | - |

`browser_navigate` 的 observation 只表示页面被打开，并且会改变标签页状态，所以可以匹配但不能直接重放。semantic ingest 会额外从 `browser_get_content` observation 提取 URL 与网页正文；最新 trace 的 6 条内容记录中，5 条正文有效，1 条空正文被跳过。

## 环境要求

- Python 3.10+
- SQLite 3
- exact-v2 无额外 Python 依赖
- OpenAI-compatible embedding provider 无额外 Python 依赖
- 本地 BGE/Sentence Transformers 需要安装 `sentence-transformers`

安装本地 embedding 运行时：

```bash
pip install sentence-transformers
```

当前机器未安装 `sentence-transformers/torch`。仓库内的 `semantic_cache.sqlite` 使用 hashing provider 构建，仅用于验证存储和检索管线，不应作为生产语义索引。

## 目录

```text
tool-reuse/
  tool_reuse/
    exact/                 exact-v2 实现
    semantic/              semantic-v1 实现
    cli.py                 CLI 入口
    hook_query.py          OpenHands pre-tool hook
  tests/                   标准库 unittest
  examples/                hook 配置片段
  docs/                    项目文档
  data/
    exact_cache.sqlite     当前 exact-v2 索引
    semantic_cache.sqlite  hashing 离线验证索引
```

## Exact Quick Start

进入项目：

```bash
cd /home/liyachen/workspace/tool-reuse
```

导入：

```bash
python3 -m tool_reuse.cli exact-ingest \
  --records /home/liyachen/workspace/experiments/traces/deep_search_2026_7_12_15_29/tool-records \
  --db /home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite
```

匹配：

```bash
python3 -m tool_reuse.cli exact-match \
  --db /home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite \
  --tool terminal \
  --input-json '{"kind":"TerminalAction","command":"curl -s https://example.com/api | head -20"}'
```

结果中的关键字段：

```json
{
  "supported": true,
  "matched": true,
  "reusable": true,
  "reason": "fresh successful replayable exact match"
}
```

`matched=true` 只表示历史中存在相同 canonical key；`reusable=true` 还要求 observation 成功、未过期且调用没有文件或浏览器状态副作用。

## Semantic Quick Start

### 本地 BGE

建索引：

```bash
python3 -m tool_reuse.cli semantic-ingest \
  --records /home/liyachen/workspace/experiments/traces/deep_search_2026_7_12_15_29/tool-records \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_bge.sqlite \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5
```

查询：

```bash
python3 -m tool_reuse.cli semantic-match \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_bge.sqlite \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5 \
  --tool terminal \
  --input-json '{"kind":"TerminalAction","command":"curl https://www.sbert.net/examples/semantic-retrieval/README.html | head -50"}'
```

### OpenAI-compatible / vLLM

```bash
export EMBEDDING_API_KEY=your-key

python3 -m tool_reuse.cli semantic-ingest \
  --records /path/to/tool-records \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_remote.sqlite \
  --provider openai-compatible \
  --base-url http://127.0.0.1:8000/v1 \
  --model BAAI/bge-m3
```

查询时必须使用相同 provider、model、dimensions 和 prefix 配置。

语义结果示例：

```json
{
  "matched": true,
  "reusable": false,
  "reason": "semantic candidates require an equivalence decision before reuse",
  "candidates": [
    {
      "record_key": "...",
      "dense_score": 0.84,
      "lexical_score": 0.93,
      "final_score": 0.89
    }
  ]
}
```

## OpenHands Hook

示例配置见 [examples/hooks.pre_tool_use.json](examples/hooks.pre_tool_use.json)。核心命令：

```json
{
  "type": "command",
  "command": "TOOL_REUSE_DB=/home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite python3 /home/liyachen/workspace/tool-reuse/tool_reuse/hook_query.py",
  "timeout": 5
}
```

hook 只查询 exact-v2：

- `reusable=true`：返回 exit code 2、`decision=deny`，并在 `additionalContext` 中加入缓存 observation；
- 其他情况：返回 `decision=allow`，工具正常执行；
- hook 内部异常：fail open，不阻断工具。

建议把 reuse hook 放在记录 hook 之前。

## 测试

```bash
cd /home/liyachen/workspace/tool-reuse
python3 -m unittest discover -s tests -v
python3 -m compileall tool_reuse
```

当前共 23 个测试，覆盖：

- curl URL/query/header/body canonicalization；
- 认证信息 hash 与入库脱敏；
- 文件副作用和 replay policy；
- browser URL/tab 状态；
- embedding 持久化和 provider/model 隔离；
- BM25 + dense hybrid 排序；
- browser page content 提取、长度限制与 URL 加权；
- fresh/stale 和 success/failure 判定。

## 安全边界

- SQLite 文件每次打开时校正为权限 `0600`；
- Authorization、cookie、API key 和 curl basic auth 不以明文写入 tool input；
- tool response 是缓存载荷，可能包含私有页面内容，应把数据库视为敏感文件；
- POST/PUT/DELETE 和文件写入操作不应仅凭相似度自动执行或重放；
- semantic hit 永远需要额外等价性判断。

## 文档

- [架构与数据流](docs/ARCHITECTURE.zh-CN.md)
- [安装与配置](docs/CONFIGURATION.zh-CN.md)
- [CLI 与返回字段](docs/CLI_REFERENCE.zh-CN.md)
- [OpenHands 集成](docs/OPENHANDS_INTEGRATION.zh-CN.md)
- [语义阈值与评测](docs/EVALUATION.zh-CN.md)
- [故障排查](docs/TROUBLESHOOTING.zh-CN.md)
- [exact-v2 详细设计](EXACT_MATCH_V2.zh-CN.md)
- [semantic-v1 详细设计](SEMANTIC_MATCH_V1.zh-CN.md)
