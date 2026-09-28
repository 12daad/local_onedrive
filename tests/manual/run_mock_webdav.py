from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import (  # noqa: E402
    MockWebDAVHandler,
    MockWebDAVHTTPServer,
    VirtualFile,
    WebDAVState,
)


def main() -> None:
    state = WebDAVState()
    state.directories.update({"quark/test", "quark/Photos"})
    state.files["quark/test/hello.txt"] = b"QuarkSync CFAPI hydration test\n"
    state.files["quark/中文 😀.txt"] = b"Unicode path works\n"
    size_mb = int(os.getenv("QUARKSYNC_MOCK_LARGE_MB", "16"))
    state.files["quark/large.bin"] = VirtualFile(size_mb * 1024 * 1024)
    server = MockWebDAVHTTPServer(("127.0.0.1", 52441), MockWebDAVHandler)
    server.state = state
    print("Mock WebDAV listening at http://127.0.0.1:52441/dav/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
