import asyncio
import phonenumbers

from phonenumbers import geocoder
from collections import Counter

from rich.table import Table
from rich.console import Console

from functions.base import TelethonFunction

console = Console()


class PhoneNumbersStatsFunc(TelethonFunction):
    """Statistics (phone numbers)"""

    async def get_phone_number(self, session):
        try:
            async with self.storage.ainitialize_session(session):
                me = await session.get_me()
                return me.phone
        except Exception:
            return

    def tally(self, phones):
        """Reduce a list of phone numbers to [(country_code, country_name, count)]."""
        countries = []
        countries_by_country_code = {}

        for phone in phones:
            if phone is not None:
                try:
                    parsed_phone = phonenumbers.parse(f"+{phone}", None)
                except Exception:
                    continue

                country = geocoder.description_for_number(parsed_phone, "en")

                if not countries_by_country_code.get(parsed_phone.country_code):
                    countries_by_country_code[parsed_phone.country_code] = country

                countries.append(parsed_phone.country_code)

        return [
            (code, countries_by_country_code[code] or "N/A", count)
            for code, count in Counter(countries).items()
        ]

    async def run(self, report):
        phones = await asyncio.gather(*[
            self.get_phone_number(session)
            for session in self.sessions
        ])

        rows = self.tally(phones)

        if not rows:
            await report("No phone numbers resolved.")
            return

        for code, name, count in rows:
            await report(f"+{code} — {name} — {count}")

    async def execute(self):
        self.ask_accounts_count()

        with console.status("Wait..."):
            phones = await asyncio.gather(*[
                self.get_phone_number(session)
                for session in self.sessions
            ])

        table = Table()

        table.add_column("Phone country code", justify="left", style="white")
        table.add_column("Country", style="white")
        table.add_column("Count", justify="center", style="white")

        for code, name, count in self.tally(phones):
            table.add_row(str(code), name, str(count))

        console.print(table)
