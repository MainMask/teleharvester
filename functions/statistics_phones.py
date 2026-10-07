import asyncio
import phonenumbers

from phonenumbers import geocoder
from collections import Counter

from rich.table import Table
from modules.console import console

from functions.base import TelethonFunction


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
        """Reduce a list of phone numbers to [(country_code, country_name, count)], a row per
        country: one code can be several (+1: the USA and Canada, +7: Russia and Kazakhstan)."""
        countries = Counter()

        for phone in phones:
            if phone is not None:
                try:
                    parsed_phone = phonenumbers.parse(f"+{phone}", None)
                except Exception:
                    continue

                # the country, not description_for_number's region (a US number's is its city)
                countries[parsed_phone.country_code, geocoder.country_name_for_number(parsed_phone, "ru")] += 1

        return [(code, name or "—", count) for (code, name), count in countries.items()]

    async def run(self, report):
        phones = await asyncio.gather(*[
            self.get_phone_number(session)
            for session in self.sessions
        ])
        # a worker on hold (busy scraping) isn't asked: the number stored in its .jsession counts it
        for session in self.on_hold:
            js = self.storage.jsessions_paths.get(self.storage.get_session_path(session))
            phones.append(str(js.account.account.phone_number).lstrip("+") if js is not None else None)

        rows = self.tally(phones)

        if not rows:
            self.progress_failed()
            await report("Ни один номер не определён.")
            return

        for code, name, count in rows:
            await report(f"+{code} — {name} — {count}")

    async def execute(self):
        self.ask_accounts_count()

        with console.status("Подождите..."):
            phones = await asyncio.gather(*[
                self.get_phone_number(session)
                for session in self.sessions
            ])

        table = Table()

        table.add_column("Код страны", justify="left", style="white")
        table.add_column("Страна", style="white")
        table.add_column("Кол-во", justify="center", style="white")

        for code, name, count in self.tally(phones):
            table.add_row(str(code), name, str(count))

        console.print(table)
