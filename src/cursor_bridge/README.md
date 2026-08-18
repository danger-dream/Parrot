# Parrot Cursor AgentService bridge

This package is Parrot's private, loopback-only adapter for Cursor OAuth account
models.  The initial Python protocol implementation was extracted from the
MIT-licensed `schultzp2020/pi-extensions` `pi-cursor` work and adapted from the
standalone `/opt/src-space/cursor-openai-proxy` prototype.

Upstream reference: <https://github.com/schultzp2020/pi-extensions/tree/main/packages/pi-cursor>

Parrot-specific changes include:

- multi-account runtime ownership and tool-call session pinning;
- OAuth token lifecycle delegated to `src.oauth_manager`;
- canonical account model catalogs with real Cursor variant resolution;
- account-native model metadata and dual-pool quota handling;
- non-streaming tool-call pause/resume support;
- private loopback authentication and lifecycle management;
- OpenAI Chat, Responses, and Anthropic ingress through Parrot's protocol layer.

Cursor AgentService is not a public inference API.  This bridge is intentionally
bound to loopback, is disabled until a Cursor account is explicitly added, and
must not be exposed as a standalone public service.
