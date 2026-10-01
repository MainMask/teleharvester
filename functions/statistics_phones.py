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

    async def execute(self):
        self.ask_accounts_count()

        with console.status("Wait..."):
            phones = await asyncio.gather(*[
                self.get_phone_number(session)
                for session in self.sessions
            ])

        countries = []
        countries_by_country_code = {}
        
        table = Table()

        table.add_column("Phone country code", justify="left", style="white")
        table.add_column("Country", style="white")
        table.add_column("Count", justify="center", style="white")

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

        countries = Counter(countries)

        for country_code, count in countries.items():
            country_name = countries_by_country_code[country_code]

            if not country_name:
                country_name = "N/A"

            table.add_row(
                str(country_code), country_name, str(count)
            )
        
        console.print(table)
