#!/usr/bin/env python3
"""局域网地址：解析 macOS 与 Linux 的命令输出，排除虚拟网卡和特殊地址，默认路由所在的网卡在前。"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import netinfo  # noqa: E402

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.6 netmask 0xffffff00 broadcast 192.168.1.255
en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.20.3.15 netmask 0xffffff00 broadcast 10.20.3.255
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 169.254.12.7 netmask 0xffff0000 broadcast 169.254.255.255
bridge100: flags=8a63<UP,BROADCAST,SMART,RUNNING,ALLMULTI,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 9000
\tinet 198.18.0.1 --> 198.18.0.1 netmask 0xfffffffc
"""
PORTS = """
Hardware Port: USB 10/100/1000 LAN
Device: en7
Ethernet Address: 00:e0:4c:68:00:01

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 84:2f:57:ad:7c:7b
"""
IP_ADDR = """2: eth0    inet 10.0.0.8/24 brd 10.0.0.255 scope global dynamic eth0\\       valid_lft 85000sec
3: wlan0    inet 192.168.31.20/24 brd 192.168.31.255 scope global dynamic wlan0\\       valid_lft 7000sec
4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever
"""


def runner(outputs: dict):
    return lambda arguments: outputs.get(arguments[0], "")


class NetinfoTests(unittest.TestCase):
    def test_macos_lists_physical_interfaces_with_the_default_route_first(self) -> None:
        run = runner({"ifconfig": IFCONFIG, "networksetup": PORTS, "route": "   route to: default\n  interface: en0\n"})
        items = netinfo.collect("darwin", run)
        self.assertEqual([("192.168.1.6", "无线", "en0"), ("10.20.3.15", "有线", "en7")],
                         [(item["address"], item["label"], item["interface"]) for item in items])

    def test_wired_comes_first_when_the_default_route_is_elsewhere(self) -> None:
        run = runner({"ifconfig": IFCONFIG, "networksetup": PORTS, "route": "  interface: utun4\n"})
        self.assertEqual(["10.20.3.15", "192.168.1.6"], [item["address"] for item in netinfo.collect("darwin", run)])

    def test_linux_names_mark_wired_and_wireless(self) -> None:
        run = runner({"ip": IP_ADDR})
        items = netinfo.collect("linux", run)
        self.assertEqual([("10.0.0.8", "有线"), ("192.168.31.20", "无线")],
                         [(item["address"], item["label"]) for item in items])

    def test_special_addresses_are_excluded(self) -> None:
        for address in ("127.0.0.1", "169.254.1.1", "198.18.0.1", "224.0.0.1", "0.0.0.0", "not-an-ip"):
            self.assertFalse(netinfo.usable(address), address)
        for address in ("192.168.1.6", "10.20.3.15", "172.16.5.4"):
            self.assertTrue(netinfo.usable(address), address)

    def test_missing_commands_give_an_empty_list(self) -> None:
        self.assertEqual([], netinfo.collect("darwin", runner({})))
        self.assertEqual([], netinfo.collect("win32", runner({})))


if __name__ == "__main__":
    unittest.main()
