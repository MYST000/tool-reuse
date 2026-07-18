# 故障排查

## 缺少 sentence-transformers

现象：创建 Sentence Transformers/BGE provider 时出现 `ModuleNotFoundError`。

在当前运行环境安装项目声明的语义依赖，并确认 OpenHands hook 与手工命令使用同一个 Python：

```bash
python3 -c 'import sentence_transformers; print(sentence_transformers.__version__)'
```

离线测试可使用 hashing provider；它不适合作为生产语义检索模型，也不能与其他 provider 建立的索引混用。

## 语义查询始终没有候选

先检查索引是否有记录，再核对索引和查询的 provider、模型、向量维度及文本构造版本。任何一项不一致都可能导致零候选或拒绝查询。还要确认工具类型过滤、最低分数和 TTL 没有排除全部记录。

## 记录存在但被判定过期

TTL 从记录采集时间与内容类别计算，不是从导入时间计算。检查系统时区、trace 时间戳和分类结果。当前默认策略见 [配置说明](CONFIGURATION.zh-CN.md)。不要通过修改系统时间或无限延长 TTL 掩盖问题；对时效内容应重新执行工具。

## browser 命中但 reusable=false

这是当前设计。`semantic-v3` 负责召回相似历史内容，固定返回 `reusable=false`。browser 查询的实体、时间和页面状态即使相似也可能不同，只有经过独立等价性与新鲜度验证后才能考虑自动复用。

## curl 看起来相同却没有精确命中

常见原因包括：

- HTTP 方法、URL scheme/host/port/path 或查询参数不同。
- request body、认证头、cookie 或影响内容协商的 header 不同。
- `curl` 参数改变了重定向、压缩、代理或 TLS 行为。
- shell 命令包含变量、管道、重定向、命令替换或多个命令，因不可安全规范化而被拒绝。
- 历史调用失败、已经过期或结果不可重放。

比较规范化后的键，不要只比较原始命令字符串。密钥应做摘要或剔除，不能以明文写入诊断日志。

## OpenAI-compatible embedding 报错

确认 endpoint 指向兼容的 `/v1/embeddings` 服务，API key、模型名和向量维度正确。分别排查 HTTP 状态码：`401/403` 通常是认证，`404` 是路径或模型，`429` 是限流，`5xx` 是服务端错误。设置连接/读取超时、有限次数指数退避，并避免对确定性的 `4xx` 无限重试。

## SQLite 无法打开或数据库损坏

检查数据库及父目录权限、磁盘空间和实际绝对路径：

```bash
sqlite3 data/exact_cache.sqlite 'PRAGMA integrity_check;'
sqlite3 data/semantic_cache.sqlite 'PRAGMA integrity_check;'
```

正常结果为 `ok`。不要让多个构建任务同时覆盖同一文件。建议离线构建临时数据库，验证后原子替换；线上读取使用只读账户或最小文件权限。

## 大响应导致索引或查询变慢

不要把无限长度正文直接用于 embedding。保留原始响应的受控副本，并为语义索引提取标题、URL、查询、关键片段和截断后的正文。设置单条响应大小上限；超限内容可以保存摘要与内容哈希。二进制、压缩包和媒体响应不应进入文本 embedding。

## Hook 失败后工具也没有执行

缓存 hook 应 fail open。确认异常路径输出 `allow`，且诊断信息写入 stderr 而不是污染 stdout JSON。还要检查 hook 命令的 Python 环境、数据库绝对路径和 stdin 事件格式。
