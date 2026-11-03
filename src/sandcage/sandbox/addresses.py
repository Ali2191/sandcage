"""Which IP addresses the fetcher may contact: public unicast only.

:func:`address_problem` returns a plain reason for any address that is not plainly public
(loopback, private, link-local and cloud metadata, shared/CGNAT, multicast, reserved,
documentation, benchmarking, unspecified), and for every IPv6 form that embeds an IPv4
address (mapped, translated, compatible, 6to4, Teredo, NAT64) it judges the embedded
address too. Anything Python's own tables call non-global is refused as well, so the two
lists back each other up. Pure logic: no DNS, no sockets.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Final

_V4: Final[tuple[tuple[IPv4Network, str], ...]] = tuple(
    (IPv4Network(net), reason)
    for net, reason in (
        ("0.0.0.0/8", "unspecified"),
        ("10.0.0.0/8", "private"),
        ("100.64.0.0/10", "shared (carrier-grade NAT)"),
        ("127.0.0.0/8", "loopback"),
        ("169.254.169.254/32", "cloud metadata"),
        ("169.254.0.0/16", "link-local"),
        ("168.63.129.16/32", "cloud platform endpoint"),
        ("172.16.0.0/12", "private"),
        ("192.0.0.0/24", "protocol-assignment"),
        ("192.0.2.0/24", "documentation"),
        ("192.88.99.0/24", "6to4 relay"),
        ("192.168.0.0/16", "private"),
        ("198.18.0.0/15", "benchmarking"),
        ("198.51.100.0/24", "documentation"),
        ("203.0.113.0/24", "documentation"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved"),
    )
)

_V6: Final[tuple[tuple[IPv6Network, str], ...]] = tuple(
    (IPv6Network(net), reason)
    for net, reason in (
        ("::/128", "unspecified"),
        ("::1/128", "loopback"),
        ("::/96", "IPv4-compatible (deprecated)"),
        ("64:ff9b:1::/48", "local-use NAT64"),
        ("100::/64", "discard-only"),
        ("2001:2::/48", "benchmarking"),
        ("2001:10::/28", "ORCHID"),
        ("2001:db8::/32", "documentation"),
        ("3fff::/20", "documentation"),
        ("5f00::/16", "segment-routing"),
        ("fc00::/7", "unique-local (private)"),
        ("fe80::/10", "link-local"),
        ("fec0::/10", "site-local (deprecated)"),
        ("ff00::/8", "multicast"),
    )
)

_NAT64: Final = IPv6Network("64:ff9b::/96")
_TRANSLATED: Final = IPv6Network("::ffff:0:0:0/96")
_LOW32: Final = 0xFFFFFFFF


def address_problem(address: str | IPv4Address | IPv6Address) -> str | None:
    """Why this address must never be contacted, or None if it is a public address."""
    if isinstance(address, str):
        try:
            parsed: IPv4Address | IPv6Address = ip_address(address)
        except ValueError:
            return "not an IP address"
    else:
        parsed = address
    if isinstance(parsed, IPv4Address):
        return _v4_problem(parsed)
    return _v6_problem(parsed)


def _v4_problem(address: IPv4Address) -> str | None:
    for network, reason in _V4:
        if address in network:
            return f"{reason} address"
    if not address.is_global or address.is_multicast:
        return "non-public address"
    return None


def _v6_problem(address: IPv6Address) -> str | None:
    if address.scope_id:
        return "scoped (interface-local) address"
    embedded = _embedded(address)
    if embedded:
        for form, inner in embedded:
            problem = _v4_problem(inner)
            if problem is not None:
                return f"{form} form of a {problem}"
        return None
    for network, reason in _V6:
        if address in network:
            return f"{reason} address"
    if not address.is_global or address.is_multicast:
        return "non-public address"
    return None


def _embedded(address: IPv6Address) -> list[tuple[str, IPv4Address]]:
    """IPv4 addresses an IPv6 address carries, with the name of the form."""
    found: list[tuple[str, IPv4Address]] = []
    if address.ipv4_mapped is not None:
        found.append(("IPv4-mapped", address.ipv4_mapped))
    if address in _TRANSLATED:
        found.append(("IPv4-translated", IPv4Address(int(address) & _LOW32)))
    if address in _NAT64:
        found.append(("NAT64", IPv4Address(int(address) & _LOW32)))
    if address.sixtofour is not None:
        found.append(("6to4", address.sixtofour))
    if address.teredo is not None:
        server, client = address.teredo
        found.extend((("Teredo server", server), ("Teredo client", client)))
    return found
