"""A federated gateway for the Agent Host Protocol.

The gateway is an AHP **host** facing the surfaces (web, Windows, CLI) and an
AHP **client** facing each node, so a developer sees one flat "my sessions
everywhere" view behind a single endpoint and a single login. The data plane
is AHP, unmodified; everything fleet-shaped (registry, per-node admission,
routing, the dial-out relay handshake) lives in this control plane.

Nothing protocol-shaped is defined here: the wire types, reducers, version
negotiation and transports come from `ahp_protocol`; the host edge
comes from `ahp_host`; the client edge comes from `ahp_client`.
"""

# The one place the version is written. `pyproject.toml` declares `version` as
# dynamic and hatch reads it from this line at build time.
__version__ = "0.1.0.dev0"

__all__ = ["__version__"]
