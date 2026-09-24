# Changelog

## [Unreleased]

### Added

- Folder browsing on Windows, using agent-host-server's Windows jail when the
  installed server has it.
- Tool calls say what they do. The line under a running call is Claude's own
  description for shell commands ("Count tracked files") or e.g. "Read file: a.py",
  instead of the host's fallback "Running Run command"; a finished call reads
  "Ran `git ls-files | wc -l`" or "Failed: Read file: a.py" instead of "Done".
- A `continueFrom` session setting: continue any of this machine's Claude Code
  conversations (terminal, IDE, Remote Control) by forking it; searchable, and
  confined to `--root`.
- A `permissionMode` session setting: Ask (`default`), Accept edits, Auto
  (Claude Code's auto mode) or Plan (plan first; approving drops to Ask), named as Claude Code names them so VS Code shows
  its icons. Kept across host restarts in the resume state.
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
- `--provider-id`, so several machines can sit behind one broker as distinct agents.
- Windows support: `file:///C:/...` URIs map to drive paths, start-up no longer
  needs POSIX signal handlers, and folder browsing is disabled (with a warning)
  where the host's jail cannot run. CI covers Linux, macOS and Windows.
