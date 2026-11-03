"""The SSRF address classifier: public unicast only, including every embedded-IPv4 form."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network, IPv6Address

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sandcage.sandbox.addresses import address_problem

BLOCKED_V4 = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
]
NETWORKS = [IPv4Network(net) for net in BLOCKED_V4]


def blocked_v4() -> st.SearchStrategy[IPv4Address]:
    return st.sampled_from(NETWORKS).flatmap(
        lambda net: st.integers(int(net.network_address), int(net.broadcast_address)).map(
            IPv4Address
        )
    )


def public_v4() -> st.SearchStrategy[IPv4Address]:
    return (
        st.integers(0, 2**32 - 1)
        .map(IPv4Address)
        .filter(lambda a: not any(a in net for net in NETWORKS))
        .filter(lambda a: str(a) != "168.63.129.16" and a.is_global)
    )


def mapped(a: IPv4Address) -> IPv6Address:
    return IPv6Address(0xFFFF << 32 | int(a))


def nat64(a: IPv4Address) -> IPv6Address:
    return IPv6Address(int(IPv6Address("64:ff9b::")) | int(a))


def sixtofour(a: IPv4Address) -> IPv6Address:
    return IPv6Address(0x2002 << 112 | int(a) << 80 | 1)


def translated(a: IPv4Address) -> IPv6Address:
    return IPv6Address(0xFFFF << 48 | int(a))


def teredo(server: IPv4Address, client: IPv4Address) -> IPv6Address:
    return IPv6Address(
        0x2001 << 112 | int(server) << 64 | 0x8000 << 48 | 0x1234 << 32 | (int(client) ^ 0xFFFFFFFF)
    )


@pytest.mark.parametrize(
    ("address", "phrase"),
    [
        ("127.0.0.1", "loopback"),
        ("10.1.2.3", "private"),
        ("172.31.255.255", "private"),
        ("192.168.0.10", "private"),
        ("169.254.169.254", "cloud metadata"),
        ("169.254.1.1", "link-local"),
        ("100.100.100.200", "shared"),
        ("0.0.0.0", "unspecified"),  # noqa: S104 - a test input, not a bind
        ("224.0.0.1", "multicast"),
        ("255.255.255.255", "reserved"),
        ("168.63.129.16", "cloud platform"),
        ("::1", "loopback"),
        ("::", "unspecified"),
        ("fe80::1", "link-local"),
        ("fe80::1%en0", "scoped"),
        ("fd00:ec2::254", "unique-local"),
        ("ff02::1", "multicast"),
        ("2001:db8::1", "documentation"),
        ("::ffff:127.0.0.1", "IPv4-mapped form of a loopback"),
        ("::ffff:169.254.169.254", "IPv4-mapped form of a cloud metadata"),
        ("64:ff9b::a9fe:a9fe", "NAT64 form of a cloud metadata"),
        ("64:ff9b::7f00:1", "NAT64 form of a loopback"),
        ("2002:7f00:1::1", "6to4 form of a loopback"),
        ("2002:c0a8:1::1", "6to4 form of a private"),
        ("::ffff:0:a00:1", "IPv4-translated form of a private"),
        ("::127.0.0.1", "IPv4-compatible"),
        ("64:ff9b:1::1", "local-use NAT64"),
        ("not-an-ip", "not an IP address"),
    ],
)
def test_never_contacted(address: str, phrase: str) -> None:
    problem = address_problem(address)
    assert problem is not None
    assert phrase in problem


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "1.1.1.1", "151.101.0.223", "2606:4700:4700::1111", "::ffff:8.8.8.8"],
)
def test_public_addresses_are_fine(address: str) -> None:
    assert address_problem(address) is None


def test_teredo_judges_both_embedded_addresses() -> None:
    public, private = IPv4Address("8.8.8.8"), IPv4Address("10.0.0.1")
    assert address_problem(teredo(public, public)) is None
    assert "Teredo server" in (address_problem(teredo(private, public)) or "")
    assert "Teredo client" in (address_problem(teredo(public, private)) or "")


@given(blocked_v4())
def test_every_blocked_ipv4_is_refused_in_every_form(address: IPv4Address) -> None:
    forms: list[IPv4Address | IPv6Address] = [
        address,
        mapped(address),
        nat64(address),
        sixtofour(address),
        translated(address),
    ]
    forms.append(teredo(IPv4Address("8.8.8.8"), address))
    for form in forms:
        assert address_problem(form) is not None, form


@given(public_v4())
def test_public_ipv4_is_allowed_in_every_embedding_form(address: IPv4Address) -> None:
    assert address_problem(address) is None
    for form in (mapped(address), nat64(address)):
        assert address_problem(form) is None, form


@given(st.integers(0, 2**128 - 1).map(IPv6Address))
def test_classifier_agrees_with_python_on_non_global_ipv6(address: IPv6Address) -> None:
    """Anything Python calls non-global is refused unless it carries a public IPv4."""
    problem = address_problem(address)
    carries_public_v4 = address.ipv4_mapped is not None or address.sixtofour is not None
    if not address.is_global and not carries_public_v4 and address.teredo is None:
        assert problem is not None, address


@given(st.text(max_size=40))
def test_never_raises_on_junk(text: str) -> None:
    result = address_problem(text)
    assert result is None or isinstance(result, str)
