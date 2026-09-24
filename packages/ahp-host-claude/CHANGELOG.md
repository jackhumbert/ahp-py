# Changelog

## [Unreleased]

### Added

- `ClaudeProvider`: Claude Code (via the Claude Agent SDK) as an AHP provider,
  with streamed text and reasoning, tool calls, usage, and resumable sessions.
- Approval policy: read-only tools run freely; edits, shell and web tools are
  confirmed by a client first, regardless of the user's Claude Code settings.
- `python -m agent_host_server_claude`: serve it on loopback with a token read
  from a file.
- Attachments on a user message reach Claude: local files and folders by path
  (with selections), images and PDFs as content blocks, text inline.
- The model picker is read from Claude Code's own list at start-up instead of
  being hard-coded; picking `default` means the account's default.
