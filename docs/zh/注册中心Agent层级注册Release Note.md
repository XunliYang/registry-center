# 注册中心 Agent 层级注册 Release Note

## 变更概述

注册中心为注册记录增加 `layer` 元数据，用于保存 Agent 所属的网络管理层级，并支持按层级查询。`layer` 不加入 AgentCard，也不参与 AgentCard 签名。

本版本采用以下固定 token：

| token | 含义 |
| --- | --- |
| `omc` | OMC 层 |
| `domain_workbench` | 域工作台层 |
| `cross_domain_coordination` | 跨域协调层 |
| `unknown` | 未声明或历史数据无法确定层级 |

一个注册记录只保存一个 `layer`。服务端对 token 做严格校验，`null`、空字符串和未定义 token 均拒绝写入或查询。

## 接口变化

现有注册和更新 URI 保持不变。注册条目支持以下新格式：

```json
{
  "agentCard": { "name": "ExampleAgent", "provider": { "organization": "example-org" } },
  "layer": "omc"
}
```

旧的直接传 AgentCard 格式继续有效。旧注册请求未提供 `layer` 时保存为 `unknown`；旧更新请求未提供 `layer` 时保留原值。显式设置 `"layer": "unknown"` 会清除原有层级。

新增两个读取接口：

```text
GET  /rest/v1/registry-center/agent-registrations/{organization}/{name}
POST /rest/v1/registry-center/agent-registrations/query
```

详情接口和查询接口返回完整注册条目：

```json
{
  "agentCard": { "name": "ExampleAgent" },
  "layer": "omc"
}
```

普通查询请求支持 `layer`、`limit`、`offset`；带非空 `task` 时执行语义查询，使用 `layer` 和 `topN`。现有 AgentCard 列表接口增加可选 `layer` 参数，响应仍为原有 `agentCards` 结构；现有语义查询接口也可增加 `layer`，响应结构不变。

主服务端口和 Integration 入口使用相同的解析、过滤和返回规则。前端历史代码不在本次改造范围内。

## 存储变更

- 关系型数据库 `agent_card` 表增加 `layer` 列，默认值为 `unknown`，并增加层级索引。PostgreSQL、GaussDB、SQLite 和 MySQL 的初始化逻辑均包含已有实例检查和迁移。
- 文件存储在现有注册元数据文件中保存 `layer`。旧元数据缺少该字段时回填为 `unknown`；非法历史值也按 `unknown` 处理并写回。
- Milvus 新集合包含 `layer` 字段；插入、更新、普通查询和语义查询均携带该字段。已有集合需要在升级窗口完成字段补齐和历史实体回填后，再启用层级过滤。

AgentCard 文件和签名内容不增加 `layer`。层级只作为注册中心元数据保存和返回。

### Milvus 历史集合迁移脚本

仓库提供 `bin/migrate-milvus-layer.py`，用于检查并迁移已有注册集合。执行前先完成集合备份，建议先使用 dry-run：

```text
python bin/migrate-milvus-layer.py --uri <milvus-uri> --collection agent_card_collection --mode auto --dry-run
```

如果已有集合没有显式的 `layer` 字段，脚本会在新集合中复制实体并将缺失或非法值归一化为 `unknown`：

```text
python bin/migrate-milvus-layer.py --uri <milvus-uri> --collection agent_card_collection --target-collection agent_card_collection_layer_v1 --mode rebuild
```

脚本不会删除或重命名源集合，也不会自动切换服务使用的集合。目标集合完成数量、主键、向量和 `layer` 校验后，再按部署方式完成集合切换。未完成迁移时，旧集合仍可执行不带 `layer` 的查询；带 `layer` 的写入和过滤请求返回服务暂不可用，避免无提示地返回不完整结果。

## 升级顺序和备份要求

升级前进入维护窗口，暂停注册、更新以及会整体重写注册元数据的后台任务，保留只读查询。

备份范围包括：

- 关系型数据库及其 schema；
- 文件存储目录中的 AgentCard、注册元数据和标签文件；
- Milvus 注册集合及集合 schema。

同时记录迁移前的 Agent 主键集合和各存储中的记录数，作为迁移后的校验基线。

推荐顺序：

1. 暂停写入并完成备份。
2. 执行关系型数据库、文件元数据和 Milvus 集合迁移，将历史数据补齐为 `unknown`。
3. 发布兼容版本，启动时检查字段、索引和枚举值。
4. 使用旧格式注册/更新、按层级查询和层级详情接口完成冒烟验证。
5. 确认主服务端口与 Integration 入口结果一致后恢复写入。

迁移校验至少应确认：Agent 总数未减少、主键集合未变化、`layer` 均为合法 token，且回填为 `unknown` 的数量可追溯。

## 回滚和兼容规则

本版本以向后兼容为主。旧客户端可以继续注册、更新和查询；未携带层级的历史数据按 `unknown` 处理。旧查询接口不改变响应结构，读取层级应使用新增接口。

语义查询的健康状态过滤和候选数量处理保持现有实现，本版本不增加候选补取逻辑。

回滚时先停止新版本写入，再恢复服务版本和数据备份。旧版本若不能保留新增元数据，不能直接恢复写入；应先确认其写入路径不会覆盖 `layer`。Milvus 集合切回迁移前版本后，层级过滤能力需明确标记为不可用，直至重新完成迁移。

`layer` 是已认证注册方声明的元数据，不代表注册中心完成了层级归属验证，也不能替代调用授权。
