# 匹配效果评估

缓存系统的目标不是单纯提高召回率，而是在避免错误复用的前提下减少真实工具调用。精确匹配与语义检索应分别评估。

## 评估集

从真实 trace 抽取查询，并为每条查询标注可复用的历史调用。评估集必须同时包含：

- 正例：参数表达不同但信息需求等价。
- 难负例：关键词高度重叠，但时间、实体、地域、认证态或写操作不同。
- 过期记录、失败记录和不可重放记录。
- 无历史匹配的查询，用于测量误复用率。

建议 JSONL 格式：

```json
{"query_id":"q-001","tool_name":"browser_search","tool_input":{"query":"OpenHands latest release"},"relevant_record_ids":["r-128"],"reusable_record_ids":[],"as_of":"2026-07-12T10:00:00+08:00","notes":"语义相关，但最新结果不能自动复用"}
{"query_id":"q-002","tool_name":"web_search","tool_input":{"kind":"SearchAction","query":"OpenHands latest release"},"relevant_record_ids":["r-201"],"reusable_record_ids":["r-201"],"as_of":"2026-07-12T10:00:00+08:00"}
```

`relevant_record_ids` 表示检索相关，`reusable_record_ids` 表示允许替代真实执行。两者必须分开标注。

## 精确匹配

精确层重点验证规范化后的等价性与安全边界：

- 相同 URL、HTTP 方法、查询参数和相关请求头应命中。
- 参数顺序、header 大小写等无语义差异的变化应按设计命中。
- 不同 body、认证上下文、方法、重要 header 或 shell 副作用必须不命中。
- 失败、过期或不可重放记录不得返回 `reusable=true`。

对规范化器建立表驱动单元测试，每个正例都至少配一个仅改变关键字段的负例。

## 语义检索指标

- `Recall@K`：存在相关记录的查询中，前 K 个候选包含相关记录的比例。
- `MRR`：第一个相关候选排名倒数的平均值。
- `Precision@K`：前 K 个候选中相关记录的比例。
- `False Reuse Rate`：本不应复用却被系统判定可复用的比例。这是自动缓存最关键的风险指标。
- `Coverage`：系统给出候选或可复用结果的查询比例。
- 节省调用率与延迟：离线回放中避免的真实调用数量及 P50/P95 查询耗时。

语义层当前不自动复用，因此主要观察 Recall@K、MRR 和人工审核精度。未来若增加自动复用分类器，应先为 False Reuse Rate 设严格上限。

## 阈值校准

余弦分数不能跨 embedding 模型直接复用。每个 `provider + model + 文本构造版本` 都要独立校准：

1. 按时间或任务划分训练、校准和测试集，避免同一 trace 泄漏。
2. 在校准集扫描阈值，绘制 precision-recall 曲线。
3. 依据业务风险选择阈值，搜索和时效数据通常要求更高精度。
4. 只在未参与调参的测试集报告最终指标。
5. 模型、模板或索引语料变化后重新校准。

生产索引与查询必须使用相同 provider、模型和文本构造版本；不一致时应拒绝比较，而不是把分数强行解释为相似度。

## Reranker

如使用 cross-encoder/LLM reranker，应同时比较：

- embedding-only 与 embedding + reranker 的 Recall@K、MRR、Precision@K。
- P50/P95 延迟、吞吐量和单次查询成本。
- 对实体、日期、否定词、HTTP 方法和参数差异的难负例表现。

reranker 只能重新排序候选，不能把副作用调用变为可自动重放。最终复用仍需独立的策略校验。

## 发布门槛

建议先以 shadow mode 运行：真实工具照常执行，同时记录缓存选择与真实结果，人工审核误命中。达到既定误复用上限后，仅对精确匹配且低风险的工具开启自动复用；browser 语义命中继续作为候选提示。
