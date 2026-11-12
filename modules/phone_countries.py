from collections import Counter

import phonenumbers
from phonenumbers import geocoder


def phone_of(storage, client, me) -> str | None:
    """An account's number: from get_me(), or as stored in its .jsession (not polled, or no answer)."""
    if me is not None and me.phone:
        return me.phone
    js = storage.jsessions_paths.get(storage.get_session_path(client))
    return str(js.account.account.phone_number) if js is not None else None


def country_of(phone) -> str | None:
    """The number's country as its ISO code — "RU", "KZ" (both +7), "US", "CA" (both +1) —; None if unknown, or a
    number of no country (Fragment's anonymous +888: "001")."""
    if not phone:
        return None
    try:
        code = phonenumbers.region_code_for_number(phonenumbers.parse(f"+{str(phone).lstrip('+')}", None))
    except Exception:
        return None
    return code if code and code.isalpha() and code != "ZZ" else None


def flag(code: str) -> str:
    """A country's ISO code as its flag: "RU" → 🇷🇺."""
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code.upper())


def summary(phones) -> str:
    """The accounts' countries, the most first: "+7 Россия — 3 · +1 Соединенные Штаты — 2"; a row per
    country, not per code (+1: the USA and Canada, +7: Russia and Kazakhstan); "" with no number known."""
    countries = Counter()
    for phone in phones:
        if phone is None:
            continue
        try:
            parsed = phonenumbers.parse(f"+{phone.lstrip('+')}", None)
        except Exception:
            continue
        # the country, not description_for_number's region (a US number's is its city)
        countries[parsed.country_code, geocoder.country_name_for_number(parsed, "ru") or "—"] += 1
    rows = sorted(countries.items(), key=lambda row: (-row[1], row[0]))
    return " · ".join(f"+{code} {name} — {count}" for (code, name), count in rows)
