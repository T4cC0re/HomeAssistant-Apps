# Changes

## 0.0.5

- Install tools through mise when the container starts.
- Prune old mise tool versions after installation.

## 0.0.4

- Stop logging every ingress request.
- Keep one live OpenCode process per user when the ingress URL changes.
- Replace a user's process only after it stops.
- Use OpenCode 2.2.5 through mise for the server.

## 0.0.3

- Allow access to the Home Assistant Core API through Supervisor.
- Log the status and response body for four API probes at startup.
- Keep the app running when a probe is denied or cannot connect.

## 0.0.2

- Start OpenCode without the Landlock file sandbox on Home Assistant OS.
- Log the Home Assistant user ID header and Supervisor watchdog requests.
- Warn when watchdog requests stop for five minutes after the first request.
- Check both Home Assistant Core proxy discovery paths with the app token.
- Stop reading LiteLLM app options when discovery is denied.

## 0.0.1

- Add a gateway with Home Assistant ingress authentication.
- Store user IDs and start one OpenCode server for each user.
- Manage tools and process restrictions with mise.
- Add LiteLLM connection discovery and a Supervisor app information fallback.
- Support ingress paths, streaming transfers, events, and WebSockets.
