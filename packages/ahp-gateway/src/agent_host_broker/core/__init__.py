"""The routing core.

Interfaces only at this layer's floor: what a node connection is, what a
session route is, and the aggregated namespace the surfaces see. Concrete
transports (`ws`) and the relay (`relay`) plug in from above; the registry
supplies node records and admission decisions from below.
"""
