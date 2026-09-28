# ADR-004: WebDAV MOVE and ETag safety

Status: accepted

OpenList advertises methods per resource and underlying storage drivers can have
different semantics. RFC 4918 does not provide a target ETag precondition for
MOVE and does not guarantee cross-backend atomicity, so capability
`atomic_move` remains false.

For a new target, QuarkSync uploads to a temporary sibling and uses MOVE with
`Overwrite:F`; a concurrently created target therefore fails instead of being
overwritten. For an existing target, QuarkSync uses direct `PUT If-Match` with
the recorded base ETag. This gives the server a single conditional write instead
of the unsafe client-side `stat -> PUT temp -> overwriting MOVE` sequence.

Some OpenList storage drivers advertise MOVE but return an error when it is
executed. For a new target only, QuarkSync then disables MOVE for the current
backend session, verifies that the destination is still absent, and uses a
direct `PUT If-None-Match: *`. If the destination appeared, the upload is
refused. Known targets never fall back to an unconditional overwrite.

If a backend ignores or rejects conditional writes, the operation fails and is
retried or becomes a visible conflict. QuarkSync never falls back to an
unconditional overwrite of a known target.
