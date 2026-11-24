"""Tests reach only this machine: a connection to any other host fails at once.

AGENTS.md §10 allows no test to use a network service. Without a guard, a test that forgot to replace a
call (Telegram getMe, a payment provider) passed or failed depending on whether the machine happened to
have internet: on GitHub's runners it reached Telegram and left a connection open in a client shared
across tests. With the guard every machine behaves the same, and the error names the host.

Loopback (the PostgreSQL test server, fake panels, the browser tests' local pages) and Unix sockets are
allowed. Proxy settings are dropped for the test run so a local proxy cannot carry traffic out either.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
from typing import Any

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


class NetworkAccessDenied(OSError):
    """A test tried to connect to a host other than this machine."""


def is_local(address: Any) -> bool:
    """Whether a socket address points at this machine (or is not an IP address at all)."""
    if not isinstance(address, tuple) or not address:
        return True  # Unix socket path or another non-IP family
    host = str(address[0]).strip("[]").split("%", 1)[0]
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def _refuse(address: Any) -> NetworkAccessDenied:
    return NetworkAccessDenied(errno.ENETUNREACH, f"tests may not reach the network (tried {address!r})")


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if self.family in (socket.AF_INET, socket.AF_INET6) and not is_local(address):
        raise _refuse(address)
    return _original_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if self.family in (socket.AF_INET, socket.AF_INET6) and not is_local(address):
        return errno.ENETUNREACH
    return _original_connect_ex(self, address)


def install() -> None:
    """Refuse non-local connections for the rest of the process."""
    for name in PROXY_VARIABLES:
        os.environ.pop(name, None)
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]  # process-wide on purpose
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]  # process-wide on purpose
