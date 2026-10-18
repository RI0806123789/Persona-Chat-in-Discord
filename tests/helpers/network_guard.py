"""SDKの通信経路が変わっても、実サービスへの接続を許可しない。"""

import ipaddress
import socket
from unittest.mock import patch


def block_network(test_case) -> None:
    """DNS・接続を遮断する。Windowsのイベントループが使うループバックだけは許す。"""
    original_lookup = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def is_local(host) -> bool:
        if host is None or host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def lookup(host, *args, **kwargs):
        if not is_local(host):
            raise AssertionError("SDKテストでの実ネットワーク通信は禁止です。")
        return original_lookup(host, *args, **kwargs)

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not is_local(address[0]):
            raise AssertionError("SDKテストでの実ネットワーク通信は禁止です。")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not is_local(address[0]):
            raise AssertionError("SDKテストでの実ネットワーク通信は禁止です。")
        return original_connect_ex(sock, address)

    for target, replacement in (
        ("socket.getaddrinfo", lookup), ("socket.socket.connect", connect),
        ("socket.socket.connect_ex", connect_ex),
    ):
        guard = patch(target, replacement)
        guard.start()
        test_case.addCleanup(guard.stop)
