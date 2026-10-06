from dataclasses import dataclass
from urllib.parse import unquote, urlparse

ACCOUNTS_PER_PROXY = 3  # how many accounts share one proxy when distributing a list
PROXY_SCHEMES = ("socks5", "socks4", "http")  # the only types Telethon accepts


@dataclass
class Proxy:
    proxy_type: str  # socks4, socks5, http
    ip: str
    port: int
    user: str | None = None
    password: str | None = None

    @classmethod
    def from_url(cls, line: str) -> "Proxy":
        """Parse a proxy URL `scheme://[user:pass@]ip:port` into a Proxy."""
        parsed = urlparse(line.strip())

        if not parsed.scheme or not parsed.hostname or not parsed.port:
            raise ValueError(f"invalid proxy: {line!r}")
        if parsed.scheme not in PROXY_SCHEMES:
            raise ValueError(f"unsupported proxy type {parsed.scheme!r} (socks5/socks4/http): {line!r}")

        return cls(
            parsed.scheme,
            parsed.hostname,
            parsed.port,
            unquote(parsed.username) if parsed.username else None,
            unquote(parsed.password) if parsed.password else None,
        )

    def as_telethon(self) -> tuple:
        if self.user and self.password:
            return (
                self.proxy_type,
                self.ip,
                self.port,
                False,
                self.user,
                self.password,
            )

        return (self.proxy_type, self.ip, self.port)


def parse_proxies(text: str) -> list["Proxy"]:
    """Parse a block of proxy lines (`scheme://[user:pass@]ip:port`) into Proxy objects.

    Blank lines and `#` comments are skipped; a malformed line raises ValueError.
    """
    proxies = []

    for line in text.splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        proxies.append(Proxy.from_url(line))

    return proxies
