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
**OpenCode** from the local apps list. The first build downloads the tools.
It needs internet access and several GB of free space.

Start the app, then select **Open Web UI**. See [DOCS.md](DOCS.md) for the
options, storage paths, and Supervisor API limits.

## Tests

Use Python 3.12 or later with `aiohttp` and `PyYAML` installed. From this
folder, run:

```sh
python -m unittest discover -s tests -v
```

The tests check ingress authentication, proxy transfers, the OpenCode v2
UI adapter, HTTP/1.1 API requests, and process reuse. The image uses the
Python version and dependencies set in the Dockerfile and `mise.toml`.

To check sandbox enforcement, run the tests inside the installed Linux app
with `OPENCODE_SANDBOX_INTEGRATION=1`. This check needs the app's AppArmor
profile and `SYS_ADMIN` capability. It creates a temporary home below
`/data`, checks isolation in a child process, then removes the home.

To check mount setup in a Linux test container with `SYS_ADMIN`, set
`OPENCODE_SANDBOX_MOUNT_TESTS=1`. This test checks the home bind mount,
hidden paths, and read-only system mounts. It also checks that the parent
process keeps its original mounts. It does not test AppArmor enforcement.
