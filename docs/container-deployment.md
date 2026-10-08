# Registry Center container deployment / 容器部署

## Image and configuration / 镜像与配置

Build with `docker build -t registry-center:local .`. The image includes only
backend code and public `etc/conf/*.example` templates. Local `server.conf`,
`persistence.conf`, certificates, signing keys, cipher keys, credentials, model
configuration, `.env`, reviews and frontend dependencies are not build inputs.
`.gcloudignore` applies the same secret exclusions to Cloud Build uploads.

镜像仅包含后端代码和公开模板，不包含开发者本地配置或凭据。修改本地配置不会
改变镜像；需要通过运行时挂载和环境变量配置，不能将私钥放进 Docker 构建上下文。

The application runs as UID **10001**. Prepare mounted data/log/config directories
for this UID. If mounting `etc/conf`, copy `server.conf.example` and
`persistence.conf.example` to the corresponding `.conf` filenames and include
`server.properties` and `log_config.conf`. The entrypoint updates `.conf` files,
so **this config mount must be writable**, with restricted permissions; it is
distinct from the read-only TLS and model mounts. Never overwrite real local
configuration with a template automatically.

`server.conf` contains feature switches and deployment/access settings;
`server.properties` contains operating parameters and business policies. Copy
both public files when provisioning a new config mount. Configure limits,
heartbeat timing and OAuth cache/scope policies in `server.properties`, or use
`REGISTRY_*` environment overrides, which Python applies after both files.
The entrypoint does not copy these policies into `server.conf`.

开关和部署接入信息放 `server.conf`，运行参数与业务策略放 `server.properties`。
初始化配置挂载目录时必须包含两者。同一键不要重复定义；现存重复会告警但保留
`server.properties` 后加载覆盖的历史行为。迁移时保留实际生效值，不用模板默认值
覆盖现场策略。环境变量最终覆盖两个文件，入口脚本不会将业务参数写回 `server.conf`。

容器用户 UID 为 **10001**。配置目录需可写（入口脚本写入平台覆盖项），数据和日志
目录同样需有相应权限。证书和模型配置可以只读挂载；证书必须容器用户可读，且
不得向其他用户公开或允许组/其他用户写入。加密配置需注入本部署自己的
`REGISTRY_CIPHER_KEY` 或只读 `REGISTRY_CIPHER_KEY_FILE`；镜像不会携带开发者的 key。

## Startup contract / 启动契约

- Default command: `serve`, including an empty argument list. It accepts no
  flags. `serve --port ...` fails rather than ignoring the argument.
- Port priority: **`PORT` > `REGISTRY_PORT` > config file**. The image supplies
  `REGISTRY_PORT=8080`; ports must be integers in `1..65535`. `PORT` is normalized
  into `REGISTRY_PORT` so the application and health probe use the same value.
- Use canonical `REGISTRY_OWNER_VALIDATION_MODE=strict|relaxed`. Legacy
  `REGISTRY_OWNER__VALIDATION__MODE` works with a deprecation warning. When both
  are explicitly set, the canonical spelling wins. The image does not bake a
  canonical owner-mode environment variable that hides a legacy strict override.
- `init` runs `python -m agent_registry.init --non-interactive`: validates
  existing configuration, identity settings and TLS material without stdin.
  The Python validator does not write configuration; the container entrypoint
  still applies runtime overrides to `.conf` files before invoking it.
  It does **not** generate certificates, migrate a
  database, test DB connectivity or validate optional model/signing services.
  `serve` performs the actual storage readiness check before binding a port.
  Bare `python -m agent_registry.init` remains interactive outside the container.
- Models: mount a complete `etc/config/models.yaml` read-only (or set
  `LLM_CONFIG_FILE`). Simplified `LLM_CHAT_MODEL` + `LLM_CHAT_URL` can generate a
  chat-only `openai_compatible` entry; `LLM_CHAT_API_KEY` is referenced by variable
  name, never written into YAML. Embedding/AOC requires the complete model file.

`PORT` 优先级最高；参数错误立即报错。`init` 是无交互配置校验，不是发证或建库命令；
Python 校验本身不写配置，但容器入口仍先写入运行时覆盖项。
默认保持 HTTPS、mTLS、所有者隔离；仅关闭 HTTPS 不会自动关闭其他安全策略。

## Production / 生产部署

The default image requires deployment-provided HTTPS certificates and client
certificate verification, with `REGISTRY_STARTUP_STRICT_IDENTITY=true`.
Certificate identity plus an HTTP listener is rejected before binding a port.
If TLS terminates at a gateway, configure `owner.identity.mode=trusted_proxy`
and explicit `owner.trusted.proxy.ips`; the gateway must authenticate clients,
strip client-supplied identity headers, set the verified identity header itself,
and be the only route to the app. Cloud Run TLS termination or IAM by itself does
not implement this per-agent identity contract. Never trust arbitrary identity
headers or use `*` for the owner identity proxy allowlist.

Mount TLS material under `/opt/registry-center/etc/ssl:ro`: server.cer,
server_key.pem (encrypted key), cert_pwd (key password), trust.cer (CA bundle).
Keep directory mode `0700` and file mode `0600`, readable by UID 10001; secure
controller-managed group-readable mounts are also accepted. Signing material
and signature validation are independent: configure/mount them according to the
security guide rather than disabling them to bypass startup errors.

For mTLS health probes, supply a dedicated client certificate/key via
`REGISTRY_HEALTHCHECK_CLIENT_CERT` and `REGISTRY_HEALTHCHECK_CLIENT_KEY`.
`REGISTRY_HEALTHCHECK_HOST` defaults to `127.0.0.1`; the server certificate must
have a matching IP SAN, or set a matching resolvable DNS name. The probe verifies
the CA and hostname and never disables TLS verification.

生产必须提供证书、签名配置及可靠身份来源。只读证书挂载不会因尝试 chmod 而导致
安全权限已满足的部署失败；无法调整且不安全的权限仍会拒绝启动。mTLS 探针必须
有专用客户端凭据，服务证书 SAN 要匹配探针主机名/IP。

## Explicit local HTTP demo / 显式本地 HTTP 演示

This disables security features deliberately and binds only to host loopback;
do not publish this profile on a public or production network.

```sh
docker run --rm -p 127.0.0.1:8080:8080 \
  -e REGISTRY_ENABLE_HTTPS=false \
  -e REGISTRY_OWNER_ISOLATION_ENABLED=false \
  -e REGISTRY_REGISTRY_SIGN_ENABLED=false \
  -e REGISTRY_SIGNATURE_VALIDATION_ENABLED=false \
  registry-center:local
```

Use the same options followed by `init` for non-interactive config validation.
To exercise the local HTTP probe while a named container is running:
`docker exec <container> python -m agent_registry.healthcheck`.

`deploy-all.ps1` refuses normal production deployment **before** creating GCP
resources: its one-click Cloud Run topology lacks a verified owner identity
gateway. Explicit `-DevelopmentOnly` disables owner isolation/signing for a demo
and uses `--no-allow-unauthenticated`; invoke it using an authorized Cloud Run IAM
identity token. Do not treat this demo as a production identity implementation.

Cloud Run 脚本默认拒绝无身份网关的生产部署；仅 `-DevelopmentOnly` 明确演示模式
可运行，并要求 Cloud Run IAM 认证，不再开放匿名访问。

## Validation / 验证

`python tests/container_smoke.py` on Linux with Docker/openssl creates an isolated
context with synthetic secret canaries, builds the real image (including forced
CRLF repair), tests `init`, invalid argv/identity rejection, platform port
precedence, real HTTP CRUD and read-only mTLS startup/ownership/health probes.
The Ubuntu `container-smoke` CI job executes this test. Bash contract/unit tests
are useful locally but are not proof that the Linux image built or ran.
