"""``python -m isales_telephony`` → run the edge daemon entry point.

Defers to :func:`isales_telephony.edge.main.run` (also exposed as the
``isales-telephony-edge`` console script). This is the preferred way
to launch the unified edge process under launchd / systemd.
"""

from __future__ import annotations

from isales_telephony.edge.main import run

if __name__ == "__main__":
    run()
