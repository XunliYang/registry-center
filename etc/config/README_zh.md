# 模型配置

模型定义放在 `etc/config/models.yaml`，该文件被 Git 忽略——从
[`models.yaml.example`](models.yaml.example) 复制一份开始。密钥不写在该文件里：
字段填写的是**保存该值的环境变量名**。本地示例见
[`.env.example`](../../.env.example)。修改后需重启进程。

`LLM_CONFIG_FILE` 可指定其他模型文件（相对路径以仓库根目录为基准；设置了但文件
不存在会直接报错）。Docker Compose 通过 `LLM_CONFIG_HOST_FILE` 挂载模型文件，该
变量只被 `docker-compose.yml` 读取，服务本身不读它。

文件必须只包含唯一一个顶层 `models:` 映射，不能有其它顶层键；能力（capability）
内的未知字段、以及本应是映射却写成标量的位置都会在读取时报错。能力名需匹配
`^[a-z][a-z0-9_]*$`，内置能力为 `chat`、`embed`、`rerank`。未写出的能力不会在
启动时校验——首次调用它时才报 “No model configured for capability ...”。

`models:` 下的每个键是一种能力（`chat`、`embed`、`rerank`，或由已注册协议
Profile 支持的其它名称）。`model` 与 `url` 必填；`provider` 默认 `openai_compatible`（`openai` 是兼容别名）
（兼容 OpenAI 请求/响应格式），`aoc_signed` 增加 AOC 签名。可选项有
`description`、`timeout`（正数秒）、`verify_ssl`、`enable_thinking`。

```yaml
models:
  chat:
    provider: openai_compatible
    model: your-model
    url: https://provider.example/v1/chat/completions
    api_key_env: LLM_CHAT_API_KEY
  embed:
    provider: aoc_signed
    model: your-embedding-model
    url: https://gateway.example/embeddings
    auth:
      app_key_env: LLM_EMBED_AUTH_APP_KEY
      app_secret_env: LLM_EMBED_AUTH_APP_SECRET
```

各可选字段及默认值：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `description` | 能力名 | 自由文本，用于日志 |
| `timeout` | `60` 秒 | 必须为正数；`0`、负数、布尔值都会被拒绝 |
| `verify_ssl` | `true` | 设为 `false` 会关闭该端点的 TLS 证书校验 |
| `enable_thinking` | `false` | 仅 `chat`；向兼容 OpenAI 的端点请求推理内容 |
| `api_key_env` | 未设置 | 填保存密钥的环境变量名；无鉴权端点可省略 |

`url` 必须是带主机名、且不含 `user:password` 的 `http://` 或 `https://` 地址。
`provider` 必须存在对应能力的已注册 Profile，否则报
`No model profile for <provider>/<capability>`。

`api_key_env` 以及所有 `*_env` 字段填的是环境变量名；直接把密钥写进文件会被
拒绝。`api_key_env` 可省略，本地无鉴权端点因此不需要它。AOC 签名还接受
`authorization_env`，以及字面量 `api_code`、`api_version`、`scenario_code`、
`scenario_version`、`ability_code`、`test_flag`。同一字段的 `*_env` 写法与字面量
写法互斥；`aoc_signed` 必须同时提供 `auth.app_key_env` 与 `auth.app_secret_env`；
给不使用 auth 的 provider 写 `auth` 块会被拒绝。

`common/llm/config/model_sources.py` 承载版本化请求/响应模板。同协议换模型
无需修改 Python；换协议则需按 `provider` 中使用的名称注册并测试新 Profile。
不要记录请求头、请求体或凭据。

容器场景：镜像不包含 `models.yaml`。文件不存在且同时设置了 `LLM_CHAT_MODEL` 与
`LLM_CHAT_URL` 时，`bin/entrypoint.sh` 会生成只含 `chat` 的 `openai_compatible`
条目；`LLM_CHAT_PROVIDER` 只允许 `openai` 或 `openai_compatible`（默认），
`LLM_CHAT_API_KEY` 只以变量名形式写入 YAML，二者只设其一会导致启动失败。
`embed` 与 `rerank` 始终需要完整的模型文件。

模型设置仍在 `.env` 里的存量部署，可运行
`python -m scripts.migrate_llm_config`。工具会写出 `models.yaml`，在改动任一
文件前先验证全部 Profile 与密钥引用，把密钥值留在 `.env`，且不输出密钥。
旧 JSON 模型文件已不再被跟踪或读取。

如果存量部署只有 `common/config/llm_config.json`，改运行
`python -m scripts.migrate_legacy_llm_json`：密钥进入 `.env`，非敏感模型定义
进入 `models.yaml`。自定义旧请求模板须先实现相应的协议 Profile。
