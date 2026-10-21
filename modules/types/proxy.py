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
            raise ValueError(f"неверный прокси: {line!r}")
        if parsed.scheme not in PROXY_SCHEMES:
            raise ValueError(f"неподдерживаемый тип прокси {parsed.scheme!r} (socks5/socks4/http): {line!r}")
        # python_socks sends a login only with its password: without one the proxy would be used unauthorized
        if parsed.username and not parsed.password:
            raise ValueError(f"логин без пароля не поддерживается: {line!r}")

        return cls(
            parsed.scheme,
            parsed.hostname,
            parsed.port,
            unquote(parsed.username) if parsed.username else None,
            unquote(parsed.password) if parsed.password else None,
        )

    def key(self) -> tuple:
        """The same proxy whatever its spelling: credentials too (rotating providers expose many
        proxies on one host:port); "" and None are the same no-auth proxy (as_telethon treats them alike)."""
        return (self.proxy_type, self.ip, self.port, self.user or None, self.password or None)

    def label(self) -> str:
        """How lists and cards show it: no credentials."""
        return f"{self.proxy_type}://{self.ip}:{self.port}"

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


def overlap(proxies, others) -> bool:
    """Any proxy of `proxies` in `others` (by Proxy.key): the pool and the restricted workers' lists
    must not share one."""
    return bool({p.key() for p in proxies} & {p.key() for p in others})


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
