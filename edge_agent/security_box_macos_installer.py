"""NexusAI one-click macOS Security Box bootstrap and daemon host."""
from __future__ import annotations

import sys

API_DEFAULT = "https://getnexusai.co.za"


def bootstrap_from_token(token: str) -> int:
    from edge_agent import security_box
    data = security_box.bootstrap(token, API_DEFAULT)
    security_box.save_site_config(str(data["site_id"]), API_DEFAULT)
    return 0


def main() -> int:
    if "--installer-token" in sys.argv:
        index = sys.argv.index("--installer-token")
        if index + 1 >= len(sys.argv):
            raise SystemExit("NexusAI installer token is missing.")
        return bootstrap_from_token(sys.argv[index + 1].strip())

    from edge_agent import security_box
    return security_box.run()


if __name__ == "__main__":
    raise SystemExit(main())
