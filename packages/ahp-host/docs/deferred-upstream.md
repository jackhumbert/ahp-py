# Deferred upstream reports

Things we have **measured** that belong upstream — in the spec
([`microsoft/agent-host-protocol`](https://github.com/microsoft/agent-host-protocol)) or in
[`microsoft/vscode`](https://github.com/microsoft/vscode) — and are **not filed yet**, because these
repos are private pre-alpha and filing invites a conversation about an implementation nobody can read.

**Filing is gated on publishing, not on confidence.** Each entry below is evidence, not a hunch: the
measurement is recorded so the report can be written from this file without re-deriving anything. When
the repos go public, work this list and move each entry to the tracker it belongs in.

Keep this list honest in both directions — if one of these turns out to be our bug, delete the entry and
say so in the changelog rather than leaving a wrong accusation lying around.

---

## U1 — The protocol cannot express "this host has no terminals" (spec)

**Measured 2026-08-03.** `AgentCapabilities` has exactly two members, `multipleChats` and
`multipleWorkingDirectories`. There is no terminal capability anywhere in root state or the initialize
result, so a client has no way to learn that a host will refuse `createTerminal`. VS Code therefore
offers the terminal affordance unconditionally, the user clicks it, and the host answers `-32009`.

The host is already doing the only part that *is* expressible: it withholds `terminalCommandPrefix`
when no backend is installed, so `!ls` is not advertised as a dead end (`host.py::_advertised_prefix`).
That asymmetry is the argument — the prefix has a "do not offer this" signal and the terminal itself
does not.

**Ask:** an `AgentCapabilities.terminals` presence flag, with the same "absent means clients MUST NOT"
semantics every other member already has, so declaring nothing keeps being the narrowest surface.

**Workaround shipped:** none possible client-side. We improved the *message* instead — see
`Denied` in `core/policy.py`, which lets a policy replace "Not permitted to create a terminal" with
something that reads as a decision. The user still has to click once.

---

## U2 — VS Code lowercases a base64 authority, breaking every agent-host file open (vscode)

**Measured 2026-08-03, and this one is a hard functional break, not a cosmetic one.** Opening any file
from a remote agent host fails:

```
Unable to read file 'vscode-agent-host://b64-d3nzoi8vywhwlmv4yw1wbguuy29t/srv/project/GATEWAY.md?_ah=…'
(Unavailable (FileSystemError): No connection for authority: b64-d3nzoi8vywhwlmv4yw1wbguuy29t)
```

The authority is `b64-` + base64 of the configured `chat.remoteAgentHosts` address. Decoded:

| | |
| --- | --- |
| `base64("wss://ahp.example.com")` | `d3NzOi8vYWhwLmV4YW1wbGUuY29t` |
| what VS Code sent | `d3nzoi8vywhwlmv4yw1wbguuy29t` |
| relationship | **the same string, lowercased** |

**Base64 is case-sensitive; a URI authority is not.** RFC 3986 §3.2.2 makes the host component
case-insensitive, so VS Code's URI normalisation lowercases it — and destroys the payload it put
there. The connection lookup then finds nothing.

**Why this is not host-side and not a permissions problem:** *zero* `resource*` requests reach the host
when this happens. It fails in the client before anything is asked. Our resource provider is installed
and working — the folder picker resolves `/srv/project`, and `resourceRead` of the same file over a direct
client returns its bytes.

**Why it affects everyone, not just us:** we checked six plausible address forms and every one
base64-encodes with 11–18 uppercase characters. Base64 of lowercase ASCII essentially always produces
uppercase, so there is no address a deployment could choose to dodge this. The `chat.remoteAgentHosts`
file-open path looks broken for any host.

**Ask:** encode the authority in something case-insensitive (base32, or lowercase hex), or carry the
address out of the authority component entirely.

**Workaround shipped:** none. Reading files through the agent host is unavailable until this is fixed
upstream; the folder picker still works because it goes through `resourceResolve`/`resourceList` on the
connection that already exists rather than minting a filesystem URI.
