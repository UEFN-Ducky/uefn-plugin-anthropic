"""Plugin tests never reach the running Ducky or UEFN, set up before any app import.

Test processes forward UI events, runs and messages to the Ducky panel at
127.0.0.1:4199 and talk to the UEFN editor listener at :4200. The session gets
its own unused port, and connections to the live ports are refused even where a
module cached the port first (a runner that imported the app before pytest).
"""
import errno
import os
import socket
import sys
from pathlib import Path

# Any test file run alone imports the app (UEFN-Ducky-Release beside this repo) as `backend`,
# and the plugin's own flat modules (`usage`, ...) by name.
_PLUGIN = Path(__file__).resolve().parent
_APP = _PLUGIN.parents[1] / "UEFN-Ducky-Release" / "ducky_app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))
if str(_PLUGIN / "backend") not in sys.path:
    sys.path.append(str(_PLUGIN / "backend"))


def pytest_configure(config):
    # Default "prepend" imports backend/test_x.py as `backend.test_x`, binding `backend` to this
    # plugin instead of the app, so most files only ran after test_claude_modes reset it.
    config.option.importmode = "importlib"
    import backend  # the app's, before pytest names a plugin test module after it

    global _app_backend
    _app_backend = backend


_app_backend = None


def _keep_app_backend():
    """``backend`` names the app's package again.

    Some test files drop the name while they load (``sys.modules.pop("backend")``) and what they
    import next is already loaded, so nothing puts it back. pytest names the next test file
    ``backend.test_x`` and filled the gap with this plugin's own backend folder: every later
    test then imported the app from the wrong package (Nov 2026: 400 of 1,033 failed in a
    whole-suite run while each file passed alone)."""
    mod = sys.modules.get("backend")
    if _app_backend is not None and str(_APP) not in str(getattr(mod, "__file__", "") or ""):
        sys.modules["backend"] = _app_backend


def pytest_collectstart(collector):  # before each test file is loaded
    _keep_app_backend()


def pytest_runtest_setup(item):
    _keep_app_backend()

_LIVE_PORTS = (4199, 4200)  # the Ducky window's panel server, the UEFN editor listener
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::")

while True:
    with socket.socket() as _panel_probe:
        _panel_probe.bind(("127.0.0.1", 0))
        _panel_test_port = _panel_probe.getsockname()[1]
    if _panel_test_port not in (*_LIVE_PORTS, 65535):
        break
os.environ["UEFN_DUCKY_PANEL_PORT"] = str(_panel_test_port + 1)  # panel server = port - 1
os.environ["UEFN_DUCKY_PORT"] = str(_panel_test_port)  # bridge's listener default
# This PC's real second Claude account stays out of the tests (failover tests opt back in).
os.environ["DUCKY_CLAUDE_FAILOVER"] = "off"


def _refuse_live_address(address):
    if (isinstance(address, tuple) and len(address) >= 2
            and str(address[0]).lower() in _LOOPBACK and address[1] in _LIVE_PORTS):
        raise ConnectionRefusedError(
            errno.ECONNREFUSED, f"tests never connect to the running Ducky or UEFN (port {address[1]})"
        )


_socket_connect = socket.socket.connect
_socket_connect_ex = socket.socket.connect_ex


def _guarded_connect(self, address):
    _refuse_live_address(address)
    return _socket_connect(self, address)


def _guarded_connect_ex(self, address):
    try:
        _refuse_live_address(address)
    except ConnectionRefusedError:
        return getattr(errno, "WSAECONNREFUSED", errno.ECONNREFUSED)
    return _socket_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
