"""本机的局域网地址：列出各网卡的 IPv4 地址并标注有线、无线，默认路由所在的网卡排在最前。只用标准库。"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import time

# 虚拟网卡与隧道（VPN、代理、虚拟机、容器）的地址学生机访问不到，不列出。
_VIRTUAL = re.compile(r"^(lo|utun|tun|tap|gif|stf|awdl|llw|anpi|ap\d|bridge|vmnet|vboxnet|docker|br-|veth|virbr|wg|zt|tailscale|ham)")
_SEARCH = ("/sbin", "/usr/sbin", "/bin", "/usr/bin")
_CACHE = {"at": 0.0, "value": []}


def _run(arguments: list) -> str:
    executable = shutil.which(arguments[0]) or next(
        (os.path.join(folder, arguments[0]) for folder in _SEARCH if os.path.exists(os.path.join(folder, arguments[0]))), None)
    if not executable:
        return ""
    try:
        result = subprocess.run([executable] + arguments[1:], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def parse_ifconfig(text: str) -> list:
    pairs, interface = [], None
    for line in text.splitlines():
        header = re.match(r"^(\S+?):\s+flags=", line)
        if header:
            interface = header.group(1)
            continue
        address = re.match(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if address and interface:
            pairs.append((interface, address.group(1)))
    return pairs


def parse_ip_addr(text: str) -> list:
    pairs = []
    for line in text.splitlines():
        match = re.match(r"^\d+:\s+(\S+?)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/", line)
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs


def parse_hardware_ports(text: str) -> dict:
    ports, name = {}, None
    for line in text.splitlines():
        if line.startswith("Hardware Port:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and name:
            ports[line.split(":", 1)[1].strip()] = name
    return ports


def parse_default_interface(text: str) -> str | None:
    match = re.search(r"interface:\s*(\S+)", text) or re.search(r"\bdev\s+(\S+)", text)
    return match.group(1) if match else None


def label(interface: str, port: str) -> str:
    text = port or ""
    if re.search(r"Wi-?Fi|AirPort|WLAN|无线", text, re.I) or (not text and interface.startswith("wl")):
        return "无线"
    if re.search(r"Ethernet|LAN|以太网", text, re.I) or (not text and re.match(r"(eth|en)", interface)):
        return "有线"
    return text


def usable(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    benchmark = ipaddress.IPv4Network("198.18.0.0/15")
    return not (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip in benchmark)


def collect(platform: str, run=_run) -> list:
    """platform 取 sys.platform；run 执行命令并返回标准输出，测试时可替换。"""
    if platform == "darwin":
        pairs = parse_ifconfig(run(["ifconfig"]))
        ports = parse_hardware_ports(run(["networksetup", "-listallhardwareports"]))
        default = parse_default_interface(run(["route", "-n", "get", "default"]))
    elif platform.startswith("linux"):
        pairs = parse_ip_addr(run(["ip", "-4", "-o", "addr", "show", "scope", "global"]))
        ports = {}
        default = parse_default_interface(run(["ip", "route", "show", "default"]))
    else:
        pairs, ports, default = [], {}, None
    items, seen = [], set()
    for interface, address in pairs:
        if _VIRTUAL.match(interface) or not usable(address) or address in seen:
            continue
        seen.add(address)
        items.append({"address": address, "label": label(interface, ports.get(interface, "")), "interface": interface})
    rank = {"有线": 1, "无线": 2}
    items.sort(key=lambda item: (item["interface"] != default, rank.get(item["label"], 3)))
    return items


def _route_address() -> str | None:
    """默认路由的源地址：UDP 套接字只选路由，不发送数据。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            address = probe.getsockname()[0]
    except OSError:
        return None
    return address if usable(address) else None


def lan_addresses(max_age: float = 30.0) -> list:
    """[{address, label, interface}]，默认路由所在的网卡在最前；结果缓存 max_age 秒。"""
    now = time.monotonic()
    if _CACHE["value"] and now - _CACHE["at"] < max_age:
        return _CACHE["value"]
    items = collect(sys.platform)
    if not items:
        address = _route_address()
        items = [{"address": address, "label": "", "interface": ""}] if address else []
    _CACHE.update(at=now, value=items)
    return items


if __name__ == "__main__":
    for entry in lan_addresses():
        print(entry["address"], entry["label"], entry["interface"])
