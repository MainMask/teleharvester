from dataclasses import dataclass


def full_name(first: str | None, last: str | None) -> str:
    """"First Last", either one left out when it is empty."""
    return " ".join(part for part in (first, last) if part)


@dataclass
class Account:
    first_name: str
    last_name: str
    user_id: int
    added_at: float  # a Unix timestamp (AccountSettings.from_client)
    phone_number: str
    username: str | None = None

