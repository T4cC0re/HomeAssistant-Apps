# T4cC0re Home Assistant Apps

Apps for Home Assistant.

| App | Description | Documentation |
| --- | --- | --- |
| [LiteLLM](litellm/README.md) | Run the LiteLLM proxy with a YAML configuration. | [Configuration](litellm/DOCS.md) |

## Install from the repository

[![Add this repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FT4cC0re%2FHomeAssistant-Apps)

To add the repository manually:

1. Open **Settings > Apps > Install app** in Home Assistant.
2. Open the menu and select **Repositories**.
3. Enter `https://github.com/T4cC0re/HomeAssistant-Apps` and select **Add**.
4. Select **LiteLLM** from this repository and select **Install**.

See the [Home Assistant repository guide](https://contributors.home-assistant.io/apps/guides/repository/) for details.

## Install a local copy

Copy the contents of `litellm/` to `/addons/local/litellm` on your Home
Assistant instance. Keep the app's `config.yaml` and `Dockerfile` directly
in that folder. Then follow the [local installation instructions](litellm/README.md).

## Run the tests

Use a Python environment with PyYAML installed. From the repository root:

```sh
cd litellm
python -m unittest discover -s tests -v
```
