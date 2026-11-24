# ahp-mcp

A remote [MCP](https://modelcontextprotocol.io) server for the
[Agent Host Protocol](https://github.com/microsoft/agent-host-protocol) (AHP).
Add it to a chat app as a custom connector, and the model there can start coding
sessions on your machines, prompt them, read what they did and stop them.

It is one more AHP client. Point it at an [`ahp-gateway`](../ahp-gateway) and
the machines are the gateway's nodes; point it at a lone [`ahp-host`](../ahp-host)
and that host is the one machine, called `host`. It depends on
[`ahp-client`](../ahp-client) and [`ahp-protocol`](../ahp-protocol), and imports
neither the host nor the gateway.

Status: pre-alpha.

## Tools

| Tool | What it does |
|---|---|
| `list_hosts` | The machines, each with its root folder, and the agents sessions can run. |
| `list_folder` | What is in a folder on a machine. |
| `list_sessions` | Your sessions on every machine, most recent first. |
| `start_session` | Start a session on a machine, in a folder, with a first prompt. |
| `send_prompt` | Send another message to a session that is not mid-turn. |
| `read_session` | A session's status, what it is waiting on, and its last turns. |
| `wait_for_session` | Block (up to five minutes) until a session stops working, and ask you when it waits on you. |
| `cancel_session` | Stop the turn a session is running. |

A folder is a path relative to the machine's root, an absolute path on it, or a
URI from a listing. Behind a gateway, naming the folder is what routes a new
session to its machine.

## When a session needs you

A session can stop to ask permission to run a tool, to have a tool's result
reviewed, or to ask a question. When the chat app supports MCP
[elicitation](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)
(it says so when it connects), `wait_for_session` turns the request into a form
in the app: the tool and its input with the session's own choices (or Allow and
Deny), or the agent's questions as fields. What you pick goes back to the
session as the AHP answer. Both ways an MCP revision carries the question work:
a request mid call, and from 2026-07-28 a result that asks and is retried.

The answer comes only from the app's form. No tool takes an answer as an
argument, so the model can follow a session but cannot approve anything for
you. Dismissing the form sends nothing, and the session keeps waiting.

When the app cannot show a form, nothing changes: the session waits for you in
any AHP client (the iOS app, VS Code), and the tools say so. A tool's sign-in,
and a question that is only a link, are always left to an AHP client.

How much a session may do without asking is the server's choice, not the
model's: `--session-config '{"permissionMode": "auto"}'` gives every session it
starts that config, and the model cannot change it.

## Sign-in

The server is meant to sit on the internet, so it is its own OAuth 2.1
authorization server, with the endpoints the
[MCP authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
asks for: protected-resource and authorization-server metadata, dynamic client
registration, PKCE, token refresh and revocation. A connector needs nothing but
the URL.

There is one resource owner: you. When an app connects, your browser lands on a
sign-in page that names the app and where it will send you back, and asks for the
owner passphrase. Nothing is issued without it. Five wrong passphrases lock the
page for fifteen minutes.

Registered clients and token digests (never the tokens) are kept in the state
file (`--state`), so a restart does not sign the app out. `--allow-redirect`
narrows which redirect URIs a client may register, for example
`--allow-redirect 'https://app.example.com/oauth/callback'`; without it any
client may register, and the passphrase is the only gate.

Embedding it instead, `build_server(fleet, public_url=..., token_verifier=...)`
takes any `mcp` `TokenVerifier`, for tokens issued by an authorization server
you already run.

## Run it

```bash
pip install "ahp-protocol @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-protocol"
pip install "ahp-client[ws] @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-client"
pip install "ahp-mcp @ git+https://github.com/jackhumbert/ahp-py#subdirectory=packages/ahp-mcp"

export AHP_MCP_ENDPOINT=wss://gateway.example.com     # your gateway (or a host)
export AHP_MCP_ENDPOINT_TOKEN=...                     # if it wants a token
export AHP_MCP_OWNER_PASSPHRASE=...                   # 12 characters or more
ahp-mcp --public-url https://mcp.example.com --session-config '{"permissionMode": "auto"}'
```

It listens on `127.0.0.1:8765`; put a TLS-terminating reverse proxy in front for
the public URL. The connector URL is the public URL plus `/mcp`
(`https://mcp.example.com/mcp`). The public URL must be the origin, with no
path: the OAuth endpoints are served at its root.

Secrets are read from the environment or from files (`--endpoint-token-file`,
`--passphrase-file`), never from arguments, which anyone on the machine can read.
`--no-auth` serves without sign-in, and only on a loopback bind.

## Development

```bash
uv sync --all-packages --all-extras --all-groups   # from the repository root
scripts/check.sh ahp-mcp
```

The tests run a real fleet in one process (stock hosts with the echo agent,
behind a real gateway) and drive it through the MCP SDK's in-process client, and
run the whole OAuth flow against the HTTP app.

## License

MIT - see [LICENSE](LICENSE).
