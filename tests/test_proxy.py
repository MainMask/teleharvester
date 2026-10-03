"""Proxy.from_url / parse_proxies: parse proxy URL strings into Proxy objects."""

import pytest

from modules.types.proxy import Proxy, parse_proxies


def test_socks5_with_auth():
    p = Proxy.from_url("socks5://user:pass@1.2.3.4:1080")
    assert p.as_telethon() == ("socks5", "1.2.3.4", 1080, False, "user", "pass")


def test_http_without_auth():
    p = Proxy.from_url("http://5.6.7.8:3128")
    assert p.as_telethon() == ("http", "5.6.7.8", 3128)


def test_whitespace_is_stripped():
    p = Proxy.from_url("  socks4://9.9.9.9:9050\n")
    assert (p.proxy_type, p.ip, p.port) == ("socks4", "9.9.9.9", 9050)


@pytest.mark.parametrize("line", ["", "1.2.3.4:1080", "socks5://1.2.3.4", "garbage"])
def test_invalid_raises(line):
    with pytest.raises(ValueError):
        Proxy.from_url(line)


def test_parse_proxies_skips_blanks_and_comments():
    ps = parse_proxies("# comment\n\nsocks5://u:p@1.1.1.1:1080\n  \nhttp://2.2.2.2:3128\n")
    assert [(p.proxy_type, p.ip, p.port) for p in ps] == [
        ("socks5", "1.1.1.1", 1080),
        ("http", "2.2.2.2", 3128),
    ]


def test_parse_proxies_empty():
    assert parse_proxies("\n# only a comment\n   \n") == []


def test_parse_proxies_bad_line_raises():
    with pytest.raises(ValueError):
        parse_proxies("socks5://u:p@1.1.1.1:1080\ngarbage\n")


@pytest.mark.parametrize("line", ["https://1.2.3.4:443", "socks5h://1.2.3.4:1080"])
def test_unsupported_scheme_raises(line):
    with pytest.raises(ValueError):
        Proxy.from_url(line)


def test_percent_encoded_credentials_are_decoded():
    p = Proxy.from_url("socks5://us%3Aer:p%40ss@1.2.3.4:1080")
    assert (p.user, p.password) == ("us:er", "p@ss")
