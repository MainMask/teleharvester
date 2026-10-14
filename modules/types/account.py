from dataclasses import dataclass


@dataclass
class Account:
    first_name: str
    last_name: str
    user_id: int
    added_at: float  # a Unix timestamp (AccountSettings.from_client)
    phone_number: str
    username: str | None = None

