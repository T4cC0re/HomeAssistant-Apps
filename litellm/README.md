# LiteLLM for Home Assistant

This app runs the LiteLLM version set in `config.yaml`.
It supports `amd64` and `aarch64` systems.

This folder contains the complete app. To install it through the app store,
add the [app repository](https://github.com/T4cC0re/HomeAssistant-Apps) to
Home Assistant, then select **LiteLLM** and **Install**.

For a local installation, copy the contents of this `litellm` folder to
`/addons/local/litellm` on your Home Assistant instance. Keep `config.yaml`
and `Dockerfile` directly at that path. Do not copy the repository root
or add another `litellm` subdirectory.

In the Home Assistant app store, select **Check for updates**. Find
**LiteLLM** under local apps, then select **Install**. Home Assistant builds
the image from the local Dockerfile.

Before you start the app, replace the default `changeme1` in the required
`master_key` password option and enter your LiteLLM YAML in the Configuration
tab. The password option always
overwrites `general_settings.master_key`; the YAML cannot set a separate key.
LiteLLM listens on container port `4000`. Host access is disabled by default.
To enable it, set a host port in the Network settings. See [DOCS.md](DOCS.md)
for instructions and an example.

The required `server_root_path` option sets the API base path and defaults
to `/v1`. The wrapper exports it as `SERVER_ROOT_PATH`. Leave this environment
variable out of your LiteLLM YAML.
Before it starts LiteLLM, the app publishes its internal hostname, port,
password option, and API path through Home Assistant discovery under the
`litellm` domain. Registration runs in the startup process.

Home Assistant passes the version from `config.yaml` as the `BUILD_VERSION`
build argument. The Dockerfile uses it for the official LiteLLM image tag
and the app version label. The startup script, `run.py`, reads
`/data/options.json`. It merges the YAML in its `DEFAULT_CONFIG` string,
your LiteLLM YAML, and the generated master key override, in that order.
It then starts LiteLLM. No database is configured by default.

From this app folder, run the wrapper tests in a Python environment with
PyYAML installed:

```sh
python -m unittest discover -s tests -v
```
