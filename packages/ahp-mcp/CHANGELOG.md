# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/), independent of the protocol's.
Every release states the protocol versions it speaks.

## [Unreleased]

## [0.1.0] - unreleased

### Added

- A remote MCP server (streamable HTTP, MCP Python SDK 2.x) whose tools drive
  AHP sessions through an `ahp-gateway` or a lone host: `list_hosts`,
  `list_folder`, `list_sessions`, `start_session`, `send_prompt`,
  `read_session`, `wait_for_session` and `cancel_session`. It speaks the AHP
  versions `ahp-protocol` 0.2.0 offers (1.1.0, 1.0.0 and 0.9.0), through `ahp-client`.
- A built-in OAuth 2.1 authorization server with one owner: metadata, dynamic
  client registration, PKCE, refresh rotation and revocation, a passphrase
  sign-in page with a lockout, an optional redirect-URI allowlist, and state
  kept as token digests across restarts. Any `mcp` `TokenVerifier` can be used
  instead.
- Approvals and questions through MCP elicitation: when the client can show a
  form, `wait_for_session` asks the user for permission to run a tool, a review
  of a tool's result, or the agent's questions, and sends the answer back to
  the session. Without elicitation the session keeps waiting for an AHP client.
- `ahp-mcp`, the command that serves it; secrets come from the environment or
  files only.
