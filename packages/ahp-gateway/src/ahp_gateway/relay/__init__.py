"""The dial-out relay.

For NAT'd nodes (laptops, roaming VMs) that the gateway cannot dial: the node
runs a small outbound registrar that dials the gateway and the gateway tunnels
AHP frames verbatim back through that pipe. This is the only place a node
runs client-shaped code, and it is transport, not AHP - the one new wire
contract the fleet owns (registration + heartbeat handshake).
"""
