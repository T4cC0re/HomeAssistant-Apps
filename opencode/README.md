# OpenCode for Home Assistant

This app starts one OpenCode server for each Home Assistant user. Open the
app through Home Assistant ingress. The gateway listens on container and
host port `8080`. It accepts user headers only from the Supervisor ingress
proxy at `172.30.32.2`. Direct access to the host port returns HTTP `403`.

The app version is set in `config.yaml`. Tool versions are set in `mise.toml`.
The app supports `amd64` and `aarch64`.

## Install

Add the [app repository](https://github.com/T4cC0re/HomeAssistant-Apps) to
Home Assistant. Select **OpenCode**, then select **Install**.

For a local installation, copy the contents of this folder to
`/addons/local/opencode`. Keep `config.yaml` and `Dockerfile` directly in
that folder. In the app store, select **Check for updates**, then install
**OpenCode** from the local apps list. The first build downloads the tools
and builds the web interface. It needs internet access and several GB of
free space.

Start the app, then select **Open Web UI**. See [DOCS.md](DOCS.md) for the
options, storage paths, and Supervisor API limits.

## Tests

Use Python 3.12 or later with `aiohttp` and `PyYAML` installed. From this
folder, run:

```sh
python -m unittest discover -s tests -v
```

A browser request test also uses Node.js when it is available. The image
uses the Python version and dependencies set in the Dockerfile and
`mise.toml`.

To check a built web interface against the pinned OpenCode server in a
browser, install Playwright in a separate test directory, then run:

```sh
python tests/browser_smoke.py \
  --web-dir /path/to/packages/app/dist \
  --opencode /path/to/opencode \
  --playwright /path/to/playwright/index.mjs \
  --browser /path/to/chrome
```

This check starts a temporary backend and gateway. It checks navigation,
API paths, assets, and the event stream through an ingress prefix.
