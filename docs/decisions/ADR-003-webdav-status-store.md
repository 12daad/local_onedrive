# ADR-003: WebDAV device-status control plane

Status: accepted

Device status is stored below `.quarksync-meta/devices` in the configured remote
root. That namespace is filtered before placeholder creation. Each device keeps
a stable UUID in its local data directory and updates a JSON heartbeat. This
avoids a center service while accepting polling latency.

