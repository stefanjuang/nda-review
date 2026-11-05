"""Run the complete current test suite without network access or real credentials."""
import os
from pathlib import Path
import socket
import sys
import unittest


def denied(*args, **kwargs):
    raise AssertionError("Network access is forbidden in offline tests.")


socket.create_connection = denied
socket.getaddrinfo = denied
for method in ("connect", "connect_ex", "sendto", "sendmsg"):
    if hasattr(socket.socket, method):
        setattr(socket.socket, method, denied)
os.environ.pop("OPENAI_API_KEY", None)
sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
