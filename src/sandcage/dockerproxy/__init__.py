"""The Docker socket proxy: the only process that holds the Docker socket.

Docker access is root on the host, so the API (the most exposed process) never gets it.
This small standard-library filter allows exactly what running hardened sandboxes needs
and refuses everything else. It imports nothing from the rest of Sandcage.
"""
