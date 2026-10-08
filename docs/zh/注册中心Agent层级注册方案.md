# 注册中心 Agent 网络管理层级注册方案（修订稿）

**状态**：Draft（需求和方案方向已确认，待字符串长度和大小写规则定稿）

**对应需求**：Requirement 25: Support Agent Registration for Different Layers

**适用范围**：注册中心服务端、主服务端口和 Integration 入口、注册中心数据库及存储适配层、注册/更新/检索接口、审计和变更事件

**暂不包含**：前端历史代码改造、A2A AgentCard 协议本身的字段扩展、独立的 Skill 级检索接口、层级声明的第三方证明、编排器内部的完整策略引擎、注册中心侧的策略校验接口

## 1. 背景与目标

注册中心需要保存 Agent 所属的网络管理层级，使调用方可以按照层级获取 Agent 候选，并为后续编排约束提供输入。Issue 31 将其描述为“在 AgentCard 注册时声明层级”，本方案将这一声明落在注册条目元数据中：标准 `a2a.types.AgentCard` 继续作为签名对象，注册条目额外携带 `layer`。改造前数据库、文件元数据和向量实体均没有层级字段。

本方案引入一个注册中心元数据字段 `layer`，其特点如下：

- 一个 Agent 只能有一个层级值。
- 层级使用单值字符串，允许不同厂商使用各自的层级命名。
- 历史数据统一使用 `unknown`。
- `layer` 作为注册条目元数据传输和保存，不进入标准 AgentCard 的签名内容。
- 注册中心根据 `layer` 执行候选过滤；编排器根据自身策略决定允许使用哪些层级。
- 层级是注册方声明的元数据，不直接作为身份认证或授权凭据。

本期注册中心只负责保存层级、按层级缩小候选集并返回层级信息，不负责判断某个编排策略是否满足跨层级调用规则。编排器拿到候选集后执行具体的允许、禁止和调用顺序判断。

## 2. 设计结论

### 2.1 AgentCard 与注册条目元数据分离

AgentCard 继续使用现有标准结构，注册中心内部将一条注册记录抽象为：

```text
AgentRegistrationItem
├── agentCard       标准 AgentCard
└── layer           注册中心元数据，单值字符串
```

推荐的新增请求条目格式为：

```json
{
  "agentCard": {
    "name": "EnergyOptimizationAgent",
    "description": "进行能耗优化",
    "url": "https://agent.example.com",
    "provider": {
      "organization": "example-org",
      "url": "https://example-org.example.com"
    }
  },
  "layer": "omc"
}
```

`agentCard` 是标准 AgentCard，`layer` 是同一注册条目的附加元数据。层级在请求进入服务端后单独解析、校验和持久化。签名校验与注册中心签名继续只针对 `agentCard` 执行。

层级放在每个注册条目中，便于同一批请求注册不同层级的 Agent；单个 Agent 仍然只能对应一个层级。

这里需要区分两个边界：

- 层级不属于 AgentCard 签名范围。
- 层级应当出现在注册条目中，供注册中心保存和检索。

如果后续要求把 `layer` 直接加入 A2A AgentCard 标准结构，需要另行评审协议字段、序列化和签名规范，本期不纳入。

层级属于注册元数据，不能单独作为身份认证或授权凭据。注册权限、所有权校验、审计和变更事件仍然是层级数据可信使用的基础。如果后续要求对层级声明本身提供可验证的第三方证明，需要另行设计签名或认证模型。

### 2.2 层级字符串

不同厂商对网络管理层级的划分和命名可能不同，注册中心不维护统一的层级枚举。`layer` 只要求是一个非空字符串，注册中心去除首尾空白后保存和精确匹配。

本期沿用 `unknown` 作为缺省字符串，用于旧客户端未提供层级、历史数据缺少层级或无法判断层级的情况。`unknown` 是约定的缺省值，不代表可选值集合；例如 `omc`、`ran_controller`、`vendor_domain_layer` 等厂商自定义值均可保存。

实现上仍保留必要的格式约束：`null`、空字符串和超过字段长度的值拒绝写入或查询；层级的大小写按原值保存和匹配。字段最大长度需要在 API 评审中最终确认，当前实现沿用 64 个字符。

层级不表达层级高低、调用方向或继承关系；编排器根据自身规则判断不同字符串之间的允许关系。

### 2.3 注册和更新接口沿用现有入口

保留现有注册和更新 URI，在请求体中增加注册条目封装，以避免把非 AgentCard 字段混入标准 AgentCard 对象。服务端同时兼容当前的旧格式。

新的条目格式建议为：

```json
{
  "agentCards": [
    {
      "agentCard": {
        "name": "EnergyOptimizationAgent",
        "description": "进行能耗优化",
        "url": "https://agent.example.com",
        "provider": {
          "organization": "example-org",
          "url": "https://example-org.example.com"
        }
      },
      "layer": "omc"
    }
  ]
}
```

旧格式继续有效：

```json
{
  "agentCards": [
    {
      "name": "EnergyOptimizationAgent",
      "description": "进行能耗优化",
      "url": "https://agent.example.com",
      "provider": {
        "organization": "example-org",
        "url": "https://example-org.example.com"
      }
    }
  ]
}
```

服务端先将两种请求归一化为内部注册条目，再执行现有 AgentCard 解析、业务校验、签名验证和持久化流程。注册条目不应同时出现 `agentCard` 和直接展开的 AgentCard 字段；混合格式返回参数错误。

为避免调用方产生“字段已经提交但实际被忽略”的误解，旧格式条目中的顶层 `layer` 字段也应视为格式错误。层级只有在新格式的 `layer` 字段中传递，服务端不得静默接收或丢弃该字段。

注册结果可以在原有结果项中增加 `layer` 字段，旧客户端通常会忽略新增响应字段：

```json
{
  "results": [
    {
      "name": "EnergyOptimizationAgent",
      "organization": "example-org",
      "status": "published",
      "layer": "omc",
      "registrySigned": true
    }
  ]
}
```

主服务端口和 integration 入口使用相同的归一化、校验和存储流程，避免两个入口对层级产生不同解释。

### 2.4 注册和更新时的缺省规则

| 操作 | `layer` 未提供 | 显式 `layer: "unknown"` | 非法值、`null`、空字符串 |
|---|---|---|---|
| 新注册 | 保存为 `unknown` | 保存为 `unknown` | 拒绝请求 |
| 更新 | 保留已有层级 | 更新为 `unknown` | 拒绝请求 |

更新接口当前对 AgentCard 使用完整替换语义。为了兼容旧客户端，旧格式更新请求未携带层级时保留数据库中的已有值。只有显式传入 `unknown` 才清除原层级。层级变更不改变 Agent 的唯一键，沿用当前部署的唯一性和 owner 语义定位原记录；`layer` 不参与记录拆分或唯一键计算。

批量请求沿用当前的顺序处理和 fail-fast 行为。若批量中的前几条已经成功，后续条目因层级非法或其他原因失败，错误响应继续返回已处理成功的条目列表。

更新实现必须区分“未提供层级”和“显式设置为 `unknown`”两种状态。关系型数据库更新时，未提供层级应从 `UPDATE` 列表中省略该列，避免先读后写造成并发覆盖；文件和向量存储需要使用现有锁、版本号或等价的并发控制机制，在完整写回时保留当前层级和其他注册元数据。AgentCard 更新与层级更新应在同一条注册记录的持久化边界内完成，任一部分失败都不能返回整体成功。

## 3. 接口设计

按主服务端口统计，本期涉及 7 个接口：1 个注册接口、3 个原有查询接口和 3 个新增层级感知查询接口。Integration 入口提供对应的 `/integration/v1` 路径。

| 类型 | 方法 | URI | 变化 | 响应 |
|---|---|---|---|---|
| AgentCard 注册 | `POST` | `/rest/v1/registry-center/agent-cards` | 兼容旧 AgentCard 输入，增加可选 layer-aware 条目格式 | 保持原结果结构，可附带 layer |
| AgentCard 列表查询 | `GET` | `/rest/v1/registry-center/agent-cards` | 增加可选 `layer` | 保持 `agentCards` |
| AgentCard 详情查询 | `GET` | `/rest/v1/registry-center/agent-cards/{organization}/{name}` | 输入和响应不变 | 保持 `agentCards` |
| AgentCard 语义查询 | `POST` | `/rest/v1/registry-center/agent-cards/semantic-query` | 请求体增加可选 `layer` | 保持 `agentCards` |
| 带层级的普通查询 | `POST` | `/rest/v1/registry-center/agent-cards-with-layer` | 新增 | `AgentCard + layer` |
| 带层级的详情查询 | `GET` | `/rest/v1/registry-center/agent-cards-with-layer/{organization}/{name}` | 新增 | `AgentCard + layer` |
| 带层级的语义查询 | `POST` | `/rest/v1/registry-center/agent-cards-with-layer/semantic-query` | 新增 | `AgentCard + layer` |

新增查询接口的返回对象保持以下结构：

```json
{
  "agentCard": { "name": "EnergyOptimizationAgent" },
  "layer": "omc"
}
```

普通查询返回对象数组和分页信息；语义查询返回对象数组和实际返回数量。原有查询接口继续返回 `agentCards` 数组，不因增加 layer 而改变响应结构。

### 3.1 现有精确查询增加可选过滤条件

现有接口：

```text
GET /rest/v1/registry-center/agent-cards
```

增加可选查询参数：

```text
layer=<string value>
```

该参数与现有 `name`、`organization` 条件按 AND 组合，按字符串精确匹配。响应继续使用原有 `agentCards` 结构，保持旧调用方的响应兼容性。未传 `layer` 时行为保持现状，结果中包含 `unknown` 数据。

示例：

```text
GET /rest/v1/registry-center/agent-cards?layer=omc
```

### 3.2 查询指定 Agent 的层级归属

Issue 31 要求运维方能够查询指定 Agent 的层级。建议新增层级感知的详情接口：

```text
GET /rest/v1/registry-center/agent-cards-with-layer/{organization}/{name}
```

返回完整注册条目：

```json
{
  "agentCard": {
    "name": "EnergyOptimizationAgent",
    "provider": {
      "organization": "example-org"
    }
  },
  "layer": "omc"
}
```

该接口沿用现有的认证、角色、owner、发布状态和健康状态判断。原有的 GET /agent-cards/{organization}/{name} 保持 agentCards 响应结构，避免旧客户端因新增元数据而改变解析逻辑。

### 3.3 增加面向编排的层级感知检索接口

增加面向编排调用方的层级感知普通查询和语义查询接口，解决“按层级查询”和“在指定层级内进行语义检索”需要返回完整注册条目的问题。接口分别为：

```text
POST /rest/v1/registry-center/agent-cards-with-layer
POST /rest/v1/registry-center/agent-cards-with-layer/semantic-query
```

两个接口都返回完整注册条目（AgentCard 加 layer），供编排方使用；现有精确查询和语义查询继续作为兼容接口保留。三类入口最终调用同一核心检索能力。

普通查询请求体：

```json
{
  "layer": "vendor_domain_layer",
  "limit": 10,
  "offset": 0
}
```

语义查询请求体：

```json
{
  "layer": "vendor_domain_layer",
  "task": "进行能耗优化",
  "topN": 10
}
```

字段约定：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `layer` | 否 | 单个非空字符串；不传表示不限制层级 |
| `task` | 语义查询必填 | 任务描述，不能为空白 |
| `topN` | 语义查询可选 | 默认 `10`，取值范围 `1..50` |
| `limit` | 普通查询可选 | 默认 `100`，取值范围 `1..1000` |
| `offset` | 普通查询可选 | 默认 `0`，必须为不小于 `0` 的整数 |

本期查询条件只接受单个 `layer` 值。Agent 的层级仍然是单值；如果编排策略允许多个层级，调用方需要分别查询后合并，或者后续另行扩展 `layers` 数组语义。`layer`、`task`、`topN`、`limit`、`offset` 均不接受 `null`；`layer` 的空字符串和超过最大长度的值返回 `422`。普通查询携带 `task` 时使用语义查询接口；语义查询接口必须携带非空 `task`。普通查询携带 `topN`，或语义查询携带 `limit`/`offset`，均返回 `422`。

返回值建议显式携带层级：

```json
{
  "agents": [
    {
      "agentCard": {
        "name": "EnergyOptimizationAgent",
        "description": "进行能耗优化",
        "url": "https://agent.example.com",
        "provider": {
          "organization": "example-org",
          "url": "https://example-org.example.com"
        }
      },
      "layer": "omc"
    }
  ],
  "count": 1
}
```

本期按 Agent 级别处理层级检索：先按 layer 筛选 Agent，再返回匹配 Agent 的完整 AgentCard，原有字段随 AgentCard 保持不变。本期不建立独立的 Skill 索引，也不新增 Skill 级别查询接口。如果需求方后续要求返回单个 Skill 结果，需要另行定义 Skill 的唯一标识、索引字段和响应结构。

检索行为如下：

- 只传 `layer`：按层级查询，返回匹配层级的已发布 Agent。
- 语义查询同时传 `task` 和 `layer`：先按层级、发布状态和健康策略过滤候选，再进行向量/LLM 检索。
- 语义查询只传 `task`：保持全层级语义检索行为，结果包含 `unknown`。
- `layer=unknown`：只匹配未知层级，不自动放宽为全量查询。
- 没有匹配结果：返回成功响应和空列表。

普通查询返回 `{ "agents": [...], "count": n, "hasMore": true|false }`，其中 `count` 是本次响应实际返回的 Agent 数量，`hasMore` 表示 `offset + count` 后是否仍有候选。普通查询使用 `limit`/`offset` 分页，按现有注册记录唯一键稳定排序；当前 owner 语义存在时按 `organization`、`name`、`owner` 排序，并统一使用 `owner` 的 `nulls last` 规则，排序值相同再使用内部记录 ID 稳定排序。语义查询返回的 `count` 同样表示实际返回数量，不承诺全量匹配总数，也不使用 `total` 表示估算值。

现有语义查询接口也可以增加可选 `layer` 请求字段，以便已有调用方逐步迁移：

```json
{
  "task": "进行能耗优化",
  "layer": "omc",
  "topN": 10
}
```

该接口仍返回原有 `agentCards`，层级感知的编排方优先使用新增接口获取完整注册条目。主服务端口现有语义接口的 `top_n` 查询参数继续兼容；integration 入口继续读取请求体中的 `topN`，并统一执行默认值 `10`、范围 `1..50` 的校验。新接口只使用请求体 `topN`，不接受 `top_n`；参数类型错误或模式不匹配返回 `422`。

新增的层级详情接口和层级感知检索接口需要同步提供主服务端口与 Integration 入口，并分别复用现有角色授权、owner 约束、审计和限流配置。Integration 入口的角色映射需要在 API 评审中明确，至少覆盖实际的编排调用方和运维查询方。

### 3.4 过滤顺序

层级条件必须在候选生成阶段生效，新接口处理顺序固定为：

```text
请求参数校验
    ↓
权限、发布状态过滤
    ↓
层级过滤
    ↓
向量检索或普通查询
    ↓
健康状态过滤（能下推时在存储层执行）
    ↓
LLM 语义排序（如果传入 task）
    ↓
topN 或 limit / offset 截取
```

对于 Milvus 等向量存储，层级条件应转换为存储层过滤表达式。先取得全局 TopN 再过滤会导致指定层级的有效 Agent 被排除，不能满足层级检索要求。

健康状态过滤继续沿用现有实现。候选不足时是否继续补取不属于本次层级注册需求，本期不调整相关逻辑；新增的 `layer` 条件只在向量检索阶段作为存储层过滤条件使用，不改变既有健康过滤和 `topN` 处理方式。

## 4. 存储模型与升级

### 4.1 关系型数据库

在 Agent 主表中增加：

```sql
layer VARCHAR(...) NOT NULL DEFAULT 'unknown'
```

关系型数据库中的列只是持久化实现。核心层的 AgentRecord、文件元数据和向量实体都必须携带同一个 layer 值，查询返回的注册条目不能只从 AgentCard JSON 推导层级。

当前实现按配置选择文件、关系型数据库或 Milvus 作为活动存储，三者并非默认同时写入的同步主库。每种部署模式都应将当前活动后端作为注册数据的事实来源，分别完成 layer 的读写、过滤和迁移；如果部署额外维护向量索引，则需要明确主存储与索引之间的同步和重建关系，不能依靠“其他后端已有 layer”补齐当前后端的数据。

当前 SQL 和 Milvus 字段使用字符串类型，最大长度暂按 64 个字符处理。数据库层不增加枚举或 `CHECK` 约束；服务端统一校验非空字符串和最大长度，保证各数据库行为一致。首尾空白去除，大小写按原值保存和匹配。

唯一键继续使用当前实现的唯一性和 owner 语义，`layer` 不加入唯一键。建议增加 `(layer, status)` 组合索引，服务于常见的“指定层级查询已发布 Agent”场景；如果 owner 参与查询条件，应结合现有 owner 索引和执行计划决定是否扩展组合索引。

升级脚本需要满足以下要求：

1. 可重复执行，已有字段和索引时不失败。
2. 为历史记录补齐 `unknown`，并将空值统一归一化为 `unknown`。
3. 新安装数据库直接创建带默认值的字段。
4. PostgreSQL、GaussDB、SQLite、MySQL 分别使用各自支持的字段检测和迁移语句。
5. 迁移完成后执行抽样校验和数量校验，确认记录总数不变。

迁移校验至少包括：新增列存在且不可为空、非空层级值未被改写、回填为 `unknown` 的数量可追溯、唯一键数量未减少、迁移前后 Agent 主键集合一致。迁移脚本和服务启动迁移不能把厂商自定义字符串截断或转换为 `unknown`。

SQLite 当前启动逻辑通过 `PRAGMA table_info` 检查已有表，并在缺少字段时执行 `ALTER TABLE`；MySQL 通过 `information_schema` 检查已有字段和索引。四种关系型数据库的启动迁移均应保持幂等，独立升级脚本可复用同一套检查逻辑。

建议为该迁移增加明确的 schema 版本或迁移标记，并在服务启动时执行能力检查。迁移未完成时，服务不能接受带 layer 的写入，也不能对外宣称已经支持层级过滤；是否允许只读启动需要由发布策略单独确定。

### 4.2 文件存储

AgentCard 文件保持原结构。层级写入现有注册中心元数据文件，与状态、所有者、标签和时间戳并列保存。读取旧元数据时缺失 `layer` 按 `unknown` 处理；启动迁移可以将缺失值回写为 `unknown`。

文件整体重写时必须保留未知字段和已有层级，避免旧版本或部分更新逻辑覆盖元数据。部署升级期间应避免使用无法保留层级字段的旧版本写入同一数据目录。

### 4.3 向量存储

向量记录增加 `layer` 字段，并纳入插入、更新、查询输出和过滤表达式。已有向量记录需要补齐 `layer=unknown`；如果当前向量数据库不支持对既有实体直接补字段，应通过重建或迁移任务完成回填。

向量检索内部返回值需要保留 AgentCard、layer、status、owner 等注册元数据，不能在向量客户端中只返回 AgentCard JSON 的部分字段。按层级返回时，服务端应返回完整 AgentCard。

向量检索和关系型/文件检索必须遵守相同的层级语义。更新实体时要保留状态、所有者及其他注册元数据，避免整体写入只更新 AgentCard 后丢失层级或状态。

本期将 Milvus 等向量存储视为启用向量检索部署的必选升级项。服务启动前必须完成 schema 检查和既有实体回填；无法原地补字段时，使用离线重建集合并在切换完成后再启用层级语义检索。未完成向量迁移的实例不得宣称已支持层级过滤。

在向量模式下，集合中的 AgentCard、layer、status、owner 等字段共同构成当前注册数据快照。层级写入成功、向量写入失败时，注册请求不能返回整体成功；迁移或重建完成后需要抽样比较 Agent 主键、layer 和 AgentCard 内容，确认集合与迁移前的注册数据一致。

## 5. 核心实现调整

建议在核心层增加统一的注册条目和层级处理能力，避免在 HTTP 路由和不同存储适配器中分别实现规则：

- 增加统一的 AgentRegistration/AgentRecord 层级模型，避免核心接口只返回 AgentCard。
- 增加“未提供 layer”的内部哨兵值，与显式 unknown 区分，避免更新时先读后写覆盖并发修改。
- `layer` 字符串及请求参数校验。
- 注册条目归一化：兼容旧 AgentCard 条目和新 `{agentCard, layer}` 条目。
- `AgentRecord` 增加 `layer` 字段。
- 注册、更新、读取、精确查询、语义检索均传递层级元数据。
- `find_exact` 增加可选层级条件。
- `retrieve_by_task` 增加可选层级条件，并保证条件在候选检索前生效。
- 普通查询为计算 `hasMore` 至少获取 `limit + 1` 条候选，返回时裁剪为 `limit`。
- 主服务端口和 integration 入口复用同一核心方法。

自定义查询和检索处理器目前通过位置参数调用核心方法。默认处理器必须把 `layer` 作为显式关键字参数传递到核心层，并把未提供和显式 `unknown` 保持为可区分状态。旧的自定义处理器可以继续处理不带层级的旧请求；当请求包含 `layer` 时，如果处理器没有声明层级能力，应返回明确的“不支持层级过滤/写入”错误，不能忽略该条件后返回成功。兼容适配应优先使用带默认值的关键字参数，避免改变已有位置参数顺序。

## 6. 权限、审计和事件

层级跟随 Agent 注册记录的所有权管理：注册者只能在自己有权限的注册/更新请求中设置或修改层级。查询是否可以查看全部层级，继续由现有接口角色和发布状态规则决定。

本期将 layer 定义为“已认证注册方声明的层级”，不定义为注册中心验证过的网络身份。编排器可以使用 layer 做候选筛选和流程约束，但不能仅凭 layer 赋予调用权限。若后续需要验证层级归属，应增加独立的验证状态或外部证明机制。

以下数据应增加层级信息：

- 注册和更新审计记录。
- Agent 注册、更新变更事件。
- 内部管理接口返回的 Agent 元数据。
- 必要的结构化日志。

AgentCard 签名验证流程保持只验证 AgentCard。层级值来自已认证的注册请求，并通过字符串格式校验和权限控制保护；它不能替代调用授权检查。

## 7. 兼容性与发布策略

建议采用“暂停写入、完成备份、迁移回填、发布兼容版本、验证、恢复写入”的发布顺序：

1. 进入维护窗口，停止注册、更新和会整体重写元数据的后台任务，保留只读查询。
2. 备份关系型数据库、文件元数据目录和向量集合，记录 Agent 主键集合及各存储记录数。
3. 执行数据库、文件存储和向量存储迁移，完成历史数据 `unknown` 回填并执行校验。
4. 发布支持旧请求格式和新注册条目格式的服务版本，启动时再次检查 schema、字段长度和层级数据完整性。
5. 通过旧格式注册、更新、精确查询和新层级查询执行冒烟验证，确认主服务端口与 integration 入口结果一致后恢复写入。
6. 编排方开始使用层级过滤接口；后续版本再根据调用方迁移情况评估是否收紧旧格式或要求新注册显式提供层级。

本方案的兼容性以向后兼容为主：旧客户端可以继续使用旧格式注册、更新和查询，新客户端通过可选 `layer` 和新增层级感知接口使用层级信息。

兼容性要求：

- 旧客户端注册不传层级时可以继续注册，层级保存为 `unknown`。
- 旧客户端更新不传层级时不能清空已有层级。
- AgentCard 签名内容和签名校验结果不因层级字段变化而改变。
- 前端历史代码不在本次改造范围内，现有基于 organization 的展示逻辑继续运行。
- 旧查询接口不传层级时保持原有返回结构和结果语义。
- 旧查询接口按 layer 过滤时仍可返回原有 AgentCard 结构；需要读取层级值的调用方使用层级详情接口或层级感知检索接口。
- 主服务端口与 Integration 入口的注册、更新、精确查询、语义查询和层级详情接口保持一致的层级语义。

降级时需要特别关注文件和向量存储的整体写入逻辑。旧版本如果无法保留新元数据，可能在更新 Agent 时丢失 `layer`。因此升级后应限制旧版本写入，或在发布前确认旧版本对新增元数据具有保留能力，并保留数据备份和回滚脚本。

回滚时先停止新版本写入，再恢复服务版本。数据库字段和文件元数据应保留，旧版本只允许读取或写入经过验证的兼容路径；如果旧版本整体写回会丢失 `layer`，不得直接恢复写入。向量集合切换失败时回退到迁移前集合，但在层级过滤能力恢复前应明确标记为不可用。

## 8. Release Note 必须包含的内容

发布说明至少包含：

- 新增 Agent 网络管理层级元数据，层级采用开放字符串；`unknown` 作为缺省值。
- `unknown` 的适用场景、历史数据回填规则，以及显式设置 `unknown` 的行为。
- 旧注册请求继续可用，未传 `layer` 时保存为 `unknown`；旧更新请求未传 `layer` 时保留原值。
- 旧查询接口保留原响应结构和结果语义，新接口返回 `AgentCard + layer`。
- 新增层级感知检索接口、层级详情接口，以及现有查询接口的可选过滤参数。
- 关系型数据库、文件存储、向量存储的升级步骤。
- 升级顺序：暂停写入、备份、迁移回填、发布兼容版本、验证、恢复写入。
- 备份范围：关系型数据库、文件元数据目录和 Milvus 集合，并记录迁移前后的主键集合及记录数。
- 回滚限制：先停止新格式写入，恢复备份并确认旧版本不会覆盖 `layer`。
- 前端历史代码暂不适配，层级由服务端和编排方接口使用。
- `layer` 是注册方声明的元数据，不代表注册中心完成了层级归属验证。

## 9. 验收标准

### 功能验收

- 单个 Agent 只能保存一个合法层级。
- 非字符串、`null`、空字符串和超过最大长度的值不能写入。
- 旧格式新注册默认保存 `unknown`。
- 旧格式更新不会清除已有层级。
- 显式更新为 `unknown` 后可按 `unknown` 查询。
- 精确查询和语义检索均支持层级过滤。
- 语义检索在层级过滤后再执行候选检索和排序。
- `unknown` 不会被误归入任意已知层级。
- 指定 Agent 的层级详情接口返回正确的 layer。
- 按层级返回完整 AgentCard，原有字段（包括 skills）保持不变；本期不提供独立 Skill 级结果。

### 存储验收

- PostgreSQL、GaussDB、SQLite、MySQL 的新安装和已有实例升级均可执行。
- 文件存储旧数据读取和迁移结果正确。
- 向量存储新旧记录均可按层级过滤。
- 迁移可重复执行，Agent 总数和唯一键不改变。
- 更新层级时 AgentCard 内容和签名行为保持正确。

### 安全和兼容性验收

- 无权限调用方不能通过层级字段绕过已有所有权和角色检查。
- 层级变化可在审计和变更事件中追踪。
- 主服务端口和 integration 入口行为一致。
- 旧客户端注册、更新和查询回归通过。
- Release Note 明确兼容规则、升级顺序、备份范围和回滚限制。
- 前端历史代码不因响应兼容字段变化而报错。

## 10. 待确认事项

以下事项不影响整体技术方向，但在进入实现前需要定稿：

1. `layer` 的最大长度和大小写匹配规则；当前实现暂按 64 个字符、大小写敏感处理。
2. 新增接口旧客户端兼容保留周期；本文已确定新增 URI 为 `agent-cards-with-layer`，层级感知查询响应使用 `agents`、`count`，普通查询使用 `limit`/`offset`。
3. `layer` 是否长期保持注册方声明语义，还是需要后续增加 `verified layer`；本文建议本期不引入验证字段。
