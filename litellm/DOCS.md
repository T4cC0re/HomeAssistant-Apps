# Configuration

Open the Configuration tab and select **Edit in YAML**. Enter the app
options in this form:

```yaml
master_key: sk-replace-with-your-own-key
server_root_path: /v1
litellm_config: |
  credential_list:
    - credential_name: openrouter
      credential_values:
        api_key: replace-with-your-provider-key
        api_base: https://openrouter.ai/api/v1
      credential_info:
        description: OpenRouter
  model_list:
    - model_name: local-model
      litellm_params:
        model: openai/your-model-name
        api_base: http://192.168.1.10:8080/v1
        api_key: "none"
        stream: true
        allowed_openai_params: [reasoning_effort]
      model_info:
        mode: chat
        supports_function_calling: true
```

Replace the example keys, model name, and address before you start the app.
Paste your complete LiteLLM configuration below `litellm_config: |`.
Keep each line indented by two spaces. Save the options, then restart the app.

## Master key

The required `master_key` password option defaults to `changeme1`. Replace
this default before you use the app. Enter the key itself. Empty values and
`os.environ/...` references are not accepted. Use this password as the API
key in your clients.

The wrapper always overwrites `general_settings.master_key` with the password
option after it merges the YAML. You cannot set a separate master key in
`litellm_config`. Any YAML value at `general_settings.master_key`, including
`null` or an environment reference, is ignored.

If you used the earlier configuration, move your master key from the LiteLLM
YAML to the password option.

## Ports

LiteLLM listens on container port `4000`. The manifest declares this port
with a `null` host mapping. Host access is disabled by default. The app
uses the internal Home Assistant network, without host networking.

To enable host access:

1. Open the app's Configuration tab and find the Network settings.
2. Show disabled ports if necessary.
3. Enter `4000`, or another available host port, for `4000/tcp`.
4. Save the settings and restart the app.

With host port `4000`, use `http://HOME_ASSISTANT_ADDRESS:4000/v1` as the API
base URL. If you select a different host port, use that port in the URL.
Use the `master_key` password option as the API key.

To disable host access, clear the host port mapping, save, and restart.
Other apps on the internal network can still use container port `4000`.
There is no ingress endpoint.

The host port is set in Network settings, not in the LiteLLM YAML. If you
used the earlier configuration, remove its top-level `port` option.
See the
[Home Assistant network configuration reference](https://contributors.home-assistant.io/apps/reference/configuration/#network).

## API base path

The required `server_root_path` option defaults to `/v1`. The wrapper exports
this option directly as `SERVER_ROOT_PATH` before it starts LiteLLM and uses
the same option for discovery.
For example, use `/api/v1` to set the API base URL to
`http://HOME_ASSISTANT_ADDRESS:4000/api/v1` when host port `4000` is enabled.
Do not append another `/v1` to the base URL.

Use an empty string or `/` for the root path. Other paths must start with `/`
and have no trailing slash. Path segments can contain letters, digits, `_`,
`-`, `.`, and `~`. Segments that are only `.` or `..` are not permitted.
Save the options and restart the app after a change.

Set the base path through `server_root_path`. Leave `SERVER_ROOT_PATH` out
of your LiteLLM YAML. LiteLLM uses this setting as its ASGI root path;
unprefixed routes remain available.

## Discovery

Before it starts the proxy, the app publishes discovery information under the
`litellm` domain. The message contains these values:

| Value | Source |
| --- | --- |
| `host` | The app's hostname on the internal Home Assistant network. |
| `port` | Container port `4000`. |
| `master_key` | The exact value of the `master_key` password option. |
| `path` | The `server_root_path` option, normally `/v1`. |

Discovery uses the internal network and works when the host port mapping
is disabled. The app publishes the full message on each start. The
Supervisor updates the existing registration when the values change.

Discovery uses the password option directly. It does not read the key from
the YAML or resolve environment references. The app does not log the key or
the Supervisor token. Startup requires the `SUPERVISOR_TOKEN` environment
variable. After the discovery attempt, the wrapper removes this variable
before it starts LiteLLM. This also applies if discovery registration fails.

Registration runs once during startup, with a ten-second request timeout.
It registers the connection details before the proxy accepts requests.
There is no background discovery process or readiness check. If registration
fails, the app logs an error and starts the proxy. Restart the app to retry.

The receiving Home Assistant integration must use the `litellm` domain and
implement an `async_step_hassio` configuration flow. This app publishes the
connection details; it does not install that integration. See the
[Home Assistant discovery guide](https://contributors.home-assistant.io/apps/guides/services-discovery/#send-a-discovery-message).

## YAML merge

The wrapper merges three layers in this order:

1.  Default values
2. Your `litellm_config` YAML from `/data/options.json`.
3. A generated mapping that sets App-specific values.

The wrapper preserves `os.environ/...` strings in the merged YAML. Replace
placeholders with your values, or use LiteLLM's environment configuration.
The YAML is passed to LiteLLM without a fixed model or credential schema.
LiteLLM validates its own settings.

## Storage

The app does not create a database or set a database URL. Models and provider
credentials come from your YAML. Features that require a database, such as
persistent virtual keys and spend history, are not configured.

Your settings can replace the database defaults, but the app does not supply a database service.
