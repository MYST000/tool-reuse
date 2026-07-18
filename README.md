# OpenHands Tool Reuse

这是一个面向 OpenHands 工具历史的本地缓存与检索实现，读取：

```text
<trace>/tool-records/tool_calls.jsonl
```

项目提供两条互补路径：

- `exact-v5`：只对明确的 Web Search 工具和搜索 URL 做严格精确匹配；
- `semantic-v3`：对只读 Web 历史使用 embedding + BM25 返回语义候选。

推荐调用顺序：

```text
新工具调用
  -> exact-v5
     -> fresh + success + replayable：返回缓存结果
     -> miss / stale / unsafe：继续
  -> semantic-v3
     -> 返回 top-k 候选
  -> equivalence judge / agent 判断
     -> 复用候选或真实执行工具
```

语义匹配不会自动复用结果。只有 exact-v5 的安全命中会被 OpenHands pre-tool hook 替换为缓存 observation。

## 当前数据

已递归发现并验证 3 份 trace：

```text
/home/liyachen/workspace/experiments/traces/
  deep_search_2026_7_8_15_48/tool-records/tool_calls.jsonl
  deep_search_2026_7_8_17_11/tool-records/tool_calls.jsonl
  deep_search_2026_7_12_15_29/tool-records/tool_calls.jsonl
```

共 149 条记录。exact-v5 在严格 key 与 provenance 策略下导入 0 条；semantic-v3 导入 55 条候选，其中 42 条显式成功。其余记录主要是有副作用或动态/runtime shell、文件编辑、思考/结束动作、状态型浏览器操作、错配 observation 或失败/不完整观察。browser 候选和 semantic hit 均固定 `reusable=false`。

## 环境要求

- Python 3.10+
- SQLite 3
- exact-v5 无额外 Python 依赖
- OpenAI-compatible embedding provider 无额外 Python 依赖
- 本地 BGE/Sentence Transformers 需要安装 `sentence-transformers`

安装本地 embedding 运行时：

```bash
pip install sentence-transformers
```

当前机器未安装 `sentence-transformers/torch`。仓库内的 `semantic_cache.sqlite` 使用 hashing provider 构建，仅用于验证存储和检索管线，不应作为生产语义索引。

历史 trace 缺少 `execution_source`。默认导入会拒绝它们；只有来源已经人工确认是原始工具执行时才使用 `--trust-legacy-origins`。新 recorder 必须写入显式 provenance，不应使用该开关。

## 目录

```text
tool-reuse/
  tool_reuse/
    exact/                 exact-v5 实现
    semantic/              semantic-v3 实现
    cli.py                 CLI 入口
    hook_query.py          OpenHands pre-tool hook
  tests/                   标准库 unittest
  examples/                hook 配置片段
  docs/                    项目文档
  data/
    exact_cache.sqlite     当前 exact-v5 索引
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
  --db /home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite \
  --scope local/default-tools/v1 \
  --trust-legacy-origins
```

匹配：

```bash
python3 -m tool_reuse.cli exact-match \
  --db /home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite \
  --scope local/default-tools/v1 \
  --tool web_search \
  --input-json '{"kind":"SearchAction","query":"OpenHands tool reuse","domains":["docs.openhands.dev"]}'
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
  --scope local/default-tools/v1 \
  --trust-legacy-origins \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5
```

查询：

```bash
python3 -m tool_reuse.cli semantic-match \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_bge.sqlite \
  --scope local/default-tools/v1 \
  --provider sentence-transformers \
  --model BAAI/bge-small-zh-v1.5 \
  --tool web_search \
  --input-json '{"kind":"SearchAction","query":"semantic retrieval embeddings"}'
```

### OpenAI-compatible / vLLM

```bash
export EMBEDDING_API_KEY=your-key

python3 -m tool_reuse.cli semantic-ingest \
  --records /path/to/tool-records \
  --db /home/liyachen/workspace/tool-reuse/data/semantic_remote.sqlite \
  --scope local/default-tools/v1 \
  --provider openai-compatible \
  --base-url http://127.0.0.1:8000/v1 \
  --model BAAI/bge-m3
```

查询时必须使用相同 scope、provider、model、dimensions 和 prefix 配置。

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
  "command": "TOOL_REUSE_DB=/home/liyachen/workspace/tool-reuse/data/exact_cache.sqlite TOOL_REUSE_SCOPE=local/default-tools/v1 python3 /home/liyachen/workspace/tool-reuse/tool_reuse/hook_query.py",
  "timeout": 5
}
```

hook 只查询 exact-v5。scope 应包含用户/租户、provider、工具版本和影响响应的配置版本：

- `reusable=true`：返回 exit code 2、`decision=deny`、结构化 `toolResponse` 和 provenance；OpenHands SDK 会生成 `execution_source=hook_replacement` 的 `ObservationEvent`；
- 其他情况：返回 `decision=allow`，工具正常执行；
- hook 内部异常：fail open，不阻断工具。

建议把 reuse hook 放在记录 hook 之前。

## 测试

```bash
cd /home/liyachen/workspace/tool-reuse
python3 -m unittest discover -s tests -v
python3 -m compileall tool_reuse
```

当前共 48 个测试，覆盖：

- Web Search query、搜索 URL 和严格只读 curl canonicalization；
- 认证调用拒绝、入库脱敏与 embedding 前文本脱敏；
- 文件、管道、动态 shell、认证和未知 curl 参数的 replay policy；
- browser 搜索 URL/tab 状态；
- Web Search query、domains 和响应参数的精确键；
- embedding 持久化和 provider/model 隔离；
- BM25 + dense hybrid 排序；
- browser page content 提取、长度限制与 URL 加权；
- fresh/stale 和 success/failure 判定。

## 安全边界

- SQLite 文件每次打开时校正为权限 `0600`；
- Authorization、cookie、API key 和 curl basic auth 不以明文写入 tool input；
- tool response 是缓存载荷，可能包含私有页面内容，应把数据库视为敏感文件；
- 普通 curl、browser 导航、POST/PUT/DELETE 和文件写入操作不进入自动复用路径；
- semantic hit 永远需要额外等价性判断。

## 文档

- [架构与数据流](docs/ARCHITECTURE.zh-CN.md)
- [安装与配置](docs/CONFIGURATION.zh-CN.md)
- [CLI 与返回字段](docs/CLI_REFERENCE.zh-CN.md)
- [OpenHands 集成](docs/OPENHANDS_INTEGRATION.zh-CN.md)
- [语义阈值与评测](docs/EVALUATION.zh-CN.md)
- [故障排查](docs/TROUBLESHOOTING.zh-CN.md)
- [exact-v5 详细设计](EXACT_MATCH_V2.zh-CN.md)
- [semantic-v3 详细设计](SEMANTIC_MATCH_V1.zh-CN.md)
