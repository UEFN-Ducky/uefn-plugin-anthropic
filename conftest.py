"""Plugin tests never reach the running Ducky or UEFN, set up before any app import.

Test processes forward UI events, runs and messages to the Ducky panel at
127.0.0.1:4199 and talk to the UEFN editor listener at :4200. The session gets
its own unused port, and connections to the live ports are refused even where a
module cached the port first (a runner that imported the app before pytest).
"""
import errno
import os
import socket

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
