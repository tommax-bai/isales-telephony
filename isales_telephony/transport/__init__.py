"""Edge-side cloud-edge transport implementations.

Concrete :class:`isales_common.transport.cloud_edge.CloudEdgeClient` impls
live here. The matching server-side implementation hosts in isales-engine
(``isales_engine.transport.grpc_server.CloudEdgeGrpcServer``).

Spec: arch-cloud-edge-split / service-communication § Requirement: 云-边
控制面 (cloud-edge gRPC bidirectional streaming).
"""
