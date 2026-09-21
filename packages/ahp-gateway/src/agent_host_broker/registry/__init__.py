"""The node registry and per-node admission.

A directory, like DNS is to HTTP: which nodes exist, how each authenticates,
and whether a given principal may start a session on a given node at all.
This layer stays offline (import-linter contract) - admission is decided
here, before AHP starts; the precedent is the current host's 403-at-handshake.
"""
