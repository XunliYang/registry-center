# Model configuration

Model definitions live in `etc/config/models.yaml`, a local file that Git
ignores — copy [`models.yaml.example`](models.yaml.example) to start. Secrets
never live there: a field names the environment variable that holds the value.
See [`.env.example`](../../.env.example) for local setup. Restart the process
after changing settings.

`LLM_CONFIG_FILE` selects a different model file (a relative path resolves
against the repository root; a path that is set but missing is an error). Docker
Compose mounts one through `LLM_CONFIG_HOST_FILE`, which is read by
`docker-compose.yml` only — the service itself never reads that variable.

The document must contain exactly one top-level `models:` mapping and nothing
else; any other top-level key, an unknown field inside a capability, or a
scalar where a mapping is expected is rejected when the file is read. Capability
names match `^[a-z][a-z0-9_]*$`; `chat`, `embed` and `rerank` are the built-in
ones. A capability that is left out is not validated at startup — the first call
that needs it fails with "No model configured for capability ...".

Each key under `models:` is a capability (`chat`, `embed`, `rerank`, or a new
name backed by a registered protocol profile). `model` and `url` are required;
`provider` defaults to `openai_compatible` (OpenAI-compatible request/response contract; `openai` remains an alias)
and `aoc_signed` adds AOC request signing. Optional fields are `description`,
`timeout` (positive seconds), `verify_ssl`, and `enable_thinking`.

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

The optional fields and their defaults:

| field | default | notes |
| --- | --- | --- |
| `description` | the capability name | free text, used in logs |
| `timeout` | `60` seconds | must be a positive number; `0`, negatives and booleans are rejected |
| `verify_ssl` | `true` | `false` disables TLS certificate verification for that endpoint |
| `enable_thinking` | `false` | `chat` only; ask an OpenAI-compatible endpoint for reasoning content |
| `api_key_env` | unset | names the environment variable holding the key; omit it for a keyless endpoint |

`url` must be `http://` or `https://` with a host and no embedded
`user:password`. `provider` must have a profile registered for that capability;
an unknown name fails with `No model profile for <provider>/<capability>`.

`api_key_env` and every `*_env` entry name an environment variable; writing a
literal secret into the file is rejected. `api_key_env` is optional, so a
keyless local endpoint needs none. The AOC profile also accepts
`authorization_env` and the literal `api_code`, `api_version`, `scenario_code`,
`scenario_version`, `ability_code`, and `test_flag`. For a given field, the
`*_env` form and the literal form are mutually exclusive, `aoc_signed` requires
both `auth.app_key_env` and `auth.app_secret_env`, and an `auth` block on a
provider that never reads it is rejected.

`common/llm/config/model_sources.py` owns the versioned request/response
profiles. Adding a model that uses an existing protocol needs no Python change;
a different wire format requires registering and testing a new profile under the
name used in `provider`. Do not log request headers, bodies, or credentials.

Containers: the image ships no `models.yaml`. When the file is absent and both
`LLM_CHAT_MODEL` and `LLM_CHAT_URL` are set, `bin/entrypoint.sh` generates a
chat-only `openai_compatible` entry; `LLM_CHAT_PROVIDER` may only be `openai` or
`openai_compatible` (the default), `LLM_CHAT_API_KEY` is referenced by name and
never written into YAML, and supplying only one of model/URL fails the start.
`embed` and `rerank` always need a complete file.

For an installation whose model settings are still in `.env`, run
`python -m scripts.migrate_llm_config`. The tool writes `models.yaml`, validates
every profile and secret reference before changing either file, leaves secret
values in `.env`, and prints no secrets. The old JSON model file is no longer
tracked or read by the application.

If an older installation still has only `common/config/llm_config.json`, run
`python -m scripts.migrate_legacy_llm_json` instead. It moves credentials to
`.env` and non-secret definitions to `models.yaml`; unsupported custom
request templates require a provider profile before migration.
