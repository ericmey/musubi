"""Refuse missing or world-open firewall sources before bootstrap mutates a host."""

from __future__ import annotations

import ipaddress
import json
import sys


def _network(name: str, value: object, *, required: bool = False) -> bool:
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ValueError(f"{name} must be set in private inventory")
        return False
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(f"{name} must be an IP address or CIDR network") from exc
    if network.prefixlen == 0 or network.network_address.is_unspecified:
        raise ValueError(f"{name} must not allow every source")
    return True


def main() -> int:
    values = json.load(sys.stdin)
    try:
        _network("musubi_admin_ssh_cidr", values.get("musubi_admin_ssh_cidr"), required=True)
        gateway = _network("musubi_kong_ip", values.get("musubi_kong_ip"))
        vlan = _network("musubi_vlan_cidr", values.get("musubi_vlan_cidr"))
        if not gateway and not vlan:
            raise ValueError("set musubi_kong_ip or musubi_vlan_cidr in private inventory")
    except ValueError as exc:
        print(f"invalid firewall source: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
