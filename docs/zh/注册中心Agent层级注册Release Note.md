# 注册中心 Agent 层级注册 Release Note

## 变更概述

注册中心为注册记录增加 `layer` 元数据，用于保存 Agent 所属的网络管理层级，并支持按层级查询和语义检索。`layer` 不加入 AgentCard，也不参与 AgentCard 签名。

`layer` 是单值字符串。不同厂商可以使用各自的层级命名，注册中心不维护统一枚举。当前字段最大长度为 64 个字符，去除首尾空白后保存和精确匹配；`null`、空字符串和超过长度限制的值拒绝写入或查询。未提供层级时保存为 `unknown`，`unknown` 只是缺省值约定。

## 接口变化

按主服务端口统计，本版本涉及 7 个接口：

| 类型 | 方法 | URI | 兼容性或用途 |
|---|---|---|---|
| AgentCard 注册 | `POST` | `/rest/v1/registry-center/agent-cards` | 保留原 URI，兼容旧 AgentCard 输入，支持 `{agentCard, layer}` 条目 |
| AgentCard 列表查询 | `GET` | `/rest/v1/registry-center/agent-cards` | 保留原输入和 `agentCards` 响应，可选 `layer` 过滤 |
| AgentCard 详情查询 | `GET` | `/rest/v1/registry-center/agent-cards/{organization}/{name}` | 保留原输入和 `agentCards` 响应 |
| AgentCard 语义查询 | `POST` | `/rest/v1/registry-center/agent-cards/semantic-query` | 保留原输入和 `agentCards` 响应，可选 `layer` 过滤 |
| 带层级的普通查询 | `POST` | `/rest/v1/registry-center/agent-cards-with-layer` | 返回 `AgentCard + layer` |
| 带层级的详情查询 | `GET` | `/rest/v1/registry-center/agent-cards-with-layer/{organization}/{name}` | 返回 `AgentCard + layer` |
| 带层级的语义查询 | `POST` | `/rest/v1/registry-center/agent-cards-with-layer/semantic-query` | 返回 `AgentCard + layer` |

主服务端口和 Integration 入口提供对应的 `/integration/v1` 路径。

旧注册请求继续有效：

```json
{
  "agentCards": [
    {
      "name": "ExampleAgent",
      "provider": { "organization": "example-org" }
    }
  ]
}
```

带层级的注册条目使用：

```json
{
  "agentCards": [
    {
      "agentCard": {
        "name": "ExampleAgent",
        "provider": { "organization": "example-org" }
      },
      "layer": "vendor_domain_layer"
    }
  ]
}
```

旧格式未提供 `layer` 时保存为 `unknown`；旧格式更新未提供 `layer` 时保留已有值。显式传入 `"layer": "unknown"` 可清除已有层级。

新增查询接口的单条响应示例：

```json
{
  "agentCard": {
    "name": "ExampleAgent",
    "provider": { "organization": "example-org" }
  },
  "layer": "vendor_domain_layer"
}
```

普通查询返回 `agents`、`count` 和 `hasMore`；语义查询返回 `agents` 和 `count`。原有查询接口继续返回 `agentCards` 数组。

## 存储变更

### 关系型数据库

`agent_card` 表增加：

```sql
layer VARCHAR(64) NOT NULL DEFAULT 'unknown'
```

PostgreSQL、GaussDB、SQLite 和 MySQL 均包含以下升级处理：

- 已有实例检查 `layer` 列，不存在时增加字段；
- 空值或空字符串回填为 `unknown`；
- 创建 `idx_agent_layer` 索引；
- 保留非空的厂商自定义字符串，不按枚举值改写；首尾空白按服务端校验规则处理。

当前 SQL 后端会在初始化时执行幂等 schema 检查和迁移。升级前仍应完成数据库备份，并在维护窗口验证记录总数和主键集合未变化。

### 文件存储

文件存储在已有注册元数据中保存 `layer`。读取旧元数据时，缺失、空值或无法使用的层级按 `unknown` 处理并回写；非空字符串保留原值。

### Milvus

Milvus 集合增加 `layer` 字段，当前最大长度为 64。插入、更新、普通查询和语义查询均携带该字段。已有集合需要运行迁移脚本：

```text
python bin/migrate-milvus-layer.py \
  --uri <milvus-uri> \
  --collection agent_card_collection \
  --mode auto \
  --dry-run
```

如果源集合没有显式 `layer` 字段，使用重建模式：

```text
python bin/migrate-milvus-layer.py \
  --uri <milvus-uri> \
  --collection agent_card_collection \
  --target-collection agent_card_collection_layer_v1 \
  --mode rebuild
```

迁移会保留已有 embedding，并为缺失或空层级写入 `unknown`。脚本不会删除源集合，也不会自动切换服务使用的集合。完成记录数、主键、向量和 layer 校验后，再切换服务配置。

## 升级顺序和备份要求

1. 进入维护窗口，暂停注册、更新和会整体重写注册元数据的后台任务。
2. 备份关系型数据库及 schema、文件存储目录、Milvus 集合和集合 schema。
3. 记录迁移前 Agent 主键集合、记录总数和缺失 layer 数量。
4. 执行关系型数据库、文件元数据和 Milvus 迁移。
5. 校验记录总数、主键集合、layer 非空性和自定义字符串保留情况。
6. 发布兼容版本，验证旧注册格式、旧查询接口和 3 个新层级查询接口。
7. 确认主服务端口与 Integration 入口结果一致后恢复写入。

## 回滚限制

回滚前先停止新版本写入。旧版本如果无法保留 `layer`，不能直接恢复写入同一份数据；应先恢复备份，或确认旧版本只读运行不会覆盖层级元数据。

Milvus 使用重建集合时，源集合保留到新集合完成校验。切回迁移前集合后，层级过滤能力不可用，直到重新完成迁移。

## 兼容规则

- AgentCard 结构和签名范围不变。
- 旧注册、更新和查询请求继续有效。
- 旧查询接口的响应结构保持不变。
- 新增接口通过独立 URI 返回 `AgentCard + layer`，不影响旧客户端解析。
- `layer` 是已认证注册方声明的元数据，不代表注册中心完成层级归属验证，也不能替代授权判断。
- 前端历史代码不在本次升级范围内。
