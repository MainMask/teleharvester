import asyncio
import os
import random
import secrets
from types import SimpleNamespace

from telethon import functions, types, utils
from telethon.errors import FreshResetAuthorisationForbiddenError, UserAlreadyParticipantError

from functions import changename, changeusername, hide_last_seen, inviting
from functions.base import TelethonFunction
from functions.base.base import AccountLimited, pick_seconds
from modules import content, profile_done, projects, tdata_import, warmup
from scraper.scrape import channel_slug
from modules.types.account import full_name

PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
READ_POSTS = (5, 20)  # a channel's latest posts viewed
READ_CHANNELS = 3  # channels already joined read on a day
BOT_ANSWER = (3, 8)  # seconds before a started bot's answer is read


class Skipped(Exception):
    """A step with nothing to do now (no content, a field kept, a chance not hit): done, said why."""


class WarmupFunc(TelethonFunction):
    """Warm up accounts"""

    async def run(self, report):
        settings = warmup.load_settings(self.storage.home)
        paths = {self.storage.get_session_path(session): session for session in self.sessions}
        due = [paths[path] for path in warmup.due(self.storage, list(paths))]
        if not due:
            await report("Некого прогревать сегодня: все прогреты, уже прогревались сегодня или ограничены.")
            return

        threads = settings["threads"]
        region = settings["region"]
        await report(f"Прогрев: аккаунтов {len(due)}, регион {content.REGIONS[region]}, потоков {threads} "
                     f"(одновременно — не больше одного на прокси, прокси: {warmup.proxy_count(self.storage)}), "
                     f"дней {settings['days']}")
        self.progress_total(len(due))
        run = SimpleNamespace(  # what the accounts warmed now share: none gets a name, bio or photo another has
            settings=settings, region=region,
            names=changename.ChangeNameFunc.current_names(self.storage),
            bios=profile_done.used("ChangeBioFunc"), photos=profile_done.used("ChangeProfilePhotoFunc"),
            usernames=set(),
        )
        semaphore = asyncio.Semaphore(threads)
        # one account per proxy at a time: the accounts of one address never warm side by side
        proxies = {}
        started = 0

        async def one(session):
            nonlocal started
            key = warmup.proxy_key(self.storage, self.storage.get_session_path(session))
            async with proxies.setdefault(key, asyncio.Lock()), semaphore:
                index, started = started, started + 1
                if index >= threads:  # the first `threads` start together, each next one after a pause
                    await asyncio.sleep(pick_seconds(self.settings.profile_pause))
                try:
                    await self.warm(session, run, report)
                except Exception as err:  # one account's trouble must not end the others'
                    self.progress_failed()
                    await report(f"[{projects.account_label(self.storage.get_session_path(session))}] ⚠️ {err}")
                self.progress_step()

        await asyncio.gather(*(one(session) for session in due))

    async def warm(self, session, run, report):
        """One day of one account's warmup: the setup steps not done yet, then the day's activity."""
        path = self.storage.get_session_path(session)
        account = warmup.key(self.storage, path)
        entry = warmup.load_state().get(account, {})
        label = projects.account_label(path)

        async def say(text):
            await report(f"[{label}] {text}")

        # went: the steps that went through, or had nothing to do (Skipped)
        done, errors, went = list(entry.get("done", [])), [], 0
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, say)
            if me is not None:
                steps = [step for step in warmup.SETUP if step not in done] + [key for key, _, _ in warmup.ACTIVITY]
                steps = [step for step in steps if run.settings["steps"][step] != warmup.OFF]
                full = None
                if any(run.settings["steps"][step] == warmup.KEEP for step in steps):
                    try:  # what is filled: a field kept must not be overwritten for want of it
                        full = (await self.call(session, functions.users.GetFullUserRequest(
                            types.InputUserSelf()))).full_user
                    except Exception as err:
                        errors.append(f"профиль не прочитан: {err}")
                        await say(f"⚠️ профиль не прочитан: {err} — шаги завтра")
                        steps = []
                for index, step in enumerate(steps):
                    if index:
                        await self.delay()
                    try:
                        await say(await getattr(self, f"_{step.replace('2fa', 'password')}")(session, me, full, run))
                    except Skipped as why:
                        await say(f"{warmup.STEPS[step]}: {why}")
                    except AccountLimited as err:  # the account can't go on today
                        errors.append(f"{warmup.STEPS[step]}: {err}")
                        await say(f"{warmup.STEPS[step]}: ⚠️ {err} — остальное завтра")
                        break
                    except Exception as err:
                        errors.append(f"{warmup.STEPS[step]}: {err}")
                        await say(f"{warmup.STEPS[step]}: ⚠️ {err}")
                        continue
                    went += 1
                    if step in warmup.SETUP:
                        done.append(step)

        if me is None:  # not polled: dead (to sessions/inactive), or failed for now
            if not await self.worker_gone(session, say):
                warmup.update(account, status=warmup.FAILED, last=warmup.today(), error="не удалось опросить")
            return

        counted = bool(went or not errors)  # a day where every step failed is no warmup day: tried again tomorrow
        day = entry.get("day", 0) + counted
        setup_left = [step for step in warmup.SETUP if run.settings["steps"][step] != warmup.OFF and step not in done]
        status = (warmup.WARMED if counted and day >= run.settings["days"] and not setup_left
                  else warmup.FAILED if errors else warmup.WARMING)
        warmup.update(account, status=status, day=day, last=warmup.today(), done=done, error="; ".join(errors),
                      **({"warmed_at": warmup.today()} if status == warmup.WARMED else {}))
        (self.progress_failed if errors else self.progress_ok)()
        await say(f"день {day}: {warmup.CATEGORIES[status]}" if counted else
                  f"день не засчитан (ни один шаг не прошёл): {warmup.CATEGORIES[status]}")

    # --- the steps: each returns what it did, or raises Skipped ---

    def _keep(self, run, step, filled):
        if run.settings["steps"][step] == warmup.KEEP and filled:
            raise Skipped("уже заполнено — оставлено")

    def _chance(self, run, setting):
        if random.randint(1, 100) > run.settings[setting]:
            raise Skipped("не выпал шанс — пропущено")

    @staticmethod
    def _pool(run, name):
        """The region's list (modules/content.py); Skipped without one."""
        if not (items := content.pool(run.region, name)):
            raise Skipped(f"нет содержимого: {content.path(run.region, name + '.txt')}")
        return items

    @staticmethod
    def _files(run, folder):
        """The region's files (modules/content.py); Skipped without them."""
        if not (items := content.files(run.region, folder)):
            raise Skipped(f"нет содержимого: {content.path(run.region, folder)}/")
        return items

    async def _name(self, session, me, full, run):
        names = self._pool(run, "names")
        first, last = changename.ChangeNameFunc.get_random_name(names, run.names)
        run.names.append(full_name(first, last))
        await self.call(session, functions.account.UpdateProfileRequest(first_name=first, last_name=last or ""))
        me.first_name, me.last_name = first, last  # the username is made of it
        self.storage.remember_name(session, first, last)
        self.mark_done(session, classname="ChangeNameFunc")
        return f"имя: {full_name(first, last)}"

    async def _username(self, session, me, full, run):
        self._keep(run, "username", me.username)
        username = await changeusername.first_free(
            session, warmup.username_candidates(me.first_name or "", me.last_name), run.usernames)
        if username is None:
            raise ValueError("не нашлось свободного username")
        await self.call(session, functions.account.UpdateUsernameRequest(username))
        self.storage.remember_username(session, username)
        self.mark_done(session, classname="ChangeUsernameFunc")
        return f"username: @{username}"

    async def _photo(self, session, me, full, run):
        self._keep(run, "photo", full and full.profile_photo)
        photos = self._files(run, "photos")
        old = await session.get_profile_photos("me")
        picked = []
        for index in range(min(warmup.count(run.settings["avatars"]), len(photos))):
            if index:
                await self.delay()
            name = profile_done.pick_fresh([os.path.basename(p) for p in photos], run.photos + picked)
            await self.call(session, functions.photos.UploadProfilePhotoRequest(
                file=await session.upload_file(warmup.unique_photo(os.path.join(os.path.dirname(photos[0]), name)))))
            picked.append(name)
        run.photos.extend(picked)
        # the new ones first, then the old ones, as 🖼 Фото does: the account is never left without an avatar
        if old:
            await self.call(session, functions.photos.DeletePhotosRequest(id=[utils.get_input_photo(p) for p in old]))
        self.mark_done(session, picked[-1], classname="ChangeProfilePhotoFunc")
        return f"аватарок: {len(picked)} (старых удалено: {len(old)})"

    async def _bio(self, session, me, full, run):
        self._keep(run, "bio", full and full.about)
        bio = profile_done.pick_fresh(self._pool(run, "bios"), run.bios)
        run.bios.append(bio)
        await self.call(session, functions.account.UpdateProfileRequest(about=bio))
        self.mark_done(session, bio, classname="ChangeBioFunc")
        return f"bio: {bio}"

    async def _music(self, session, me, full, run):
        self._keep(run, "music", full and full.saved_music)
        self._chance(run, "music_chance")
        track = random.choice(self._files(run, "music"))
        performer, _, title = os.path.splitext(os.path.basename(track))[0].rpartition(" - ")
        message = await session.send_file("me", track, attributes=[
            types.DocumentAttributeAudio(duration=0, title=title, performer=performer or None)])
        try:
            document = message.media.document
            await self.call(session, functions.account.SaveMusicRequest(id=types.InputDocument(
                document.id, document.access_hash, document.file_reference)))
        finally:
            await message.delete()
        return f"музыка в профиле: {os.path.basename(track)}"

    async def _stickers(self, session, me, full, run):
        self._chance(run, "stickers_chance")
        name = random.choice(self._pool(run, "stickers"))
        await self.call(session, functions.messages.InstallStickerSetRequest(
            stickerset=types.InputStickerSetShortName(name.rsplit("/", 1)[-1]), archived=False))
        return f"стикерпак: {name}"

    async def _lastseen(self, session, me, full, run):
        await self.call(session, hide_last_seen.HIDE)
        self.mark_done(session, classname="HideLastSeenFunc")
        return "последний визит скрыт"

    async def _password(self, session, me, full, run):
        if self.storage.jsessions_paths.get(self.storage.get_session_path(session)) is None:
            raise Skipped("у .session-воркера негде сохранить пароль — пропущено")  # a random one would be lost
        if (await self.call(session, functions.account.GetPasswordRequest())).has_password:
            raise Skipped("пароль 2FA уже стоит")
        password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(14))
        await session.edit_2fa(new_password=password)
        tdata_import.remember_password(self.storage, session, password)
        self.mark_done(session, classname="SetPasswordFunc")
        return "пароль 2FA поставлен и сохранён в файле сессии"

    async def _terminate(self, session, me, full, run):
        try:
            await self.call(session, functions.auth.ResetAuthorizationsRequest())
        except FreshResetAuthorisationForbiddenError:
            raise ValueError("Telegram не даёт новой сессии сбросить чужие — повтор завтра")
        self.mark_done(session, classname="TerminateSessionsFunc")
        return "чужие сессии сброшены"

    async def _channels(self, session, me, full, run):
        joined = []
        for link in self._join_picks(run):
            slug = channel_slug(link)  # as 🚪 Вступить в чат parses it: @name, t.me/name, an invite's +hash
            request = (functions.messages.ImportChatInviteRequest(slug[1:]) if slug.startswith("+")
                       else functions.channels.JoinChannelRequest("@" + slug))
            try:  # the result wraps its Updates (InvitingFunc.joined_chat); none without its chat
                if chat := inviting.InvitingFunc.joined_chat(await self.call(session, request)):
                    joined.append(chat)
            except UserAlreadyParticipantError:  # an invite's chat it is in already: read with the others
                pass
            await self.delay()
        ids = {channel.id for channel in joined}
        read = [dialog.entity async for dialog in session.iter_dialogs(limit=50)
                if dialog.is_channel and dialog.unread_count and dialog.entity.id not in ids]
        for channel in joined + random.sample(read, k=min(READ_CHANNELS, len(read))):
            posts = await session.get_messages(channel, limit=random.randint(*READ_POSTS))
            if posts:
                await self.call(session, functions.messages.GetMessagesViewsRequest(
                    peer=channel, id=[post.id for post in posts], increment=True))
                await session.send_read_acknowledge(channel, max_id=posts[0].id)
            await self.delay()
        return f"вступил в каналов: {len(joined)}, прочитано: {len(joined) + min(READ_CHANNELS, len(read))}"

    @staticmethod
    def _join_picks(run) -> list[str]:
        """Today's channels to join, from the pool (none from an empty one: then only the joined ones are read)."""
        links = content.pool(run.region, "channels")
        return random.sample(links, k=min(warmup.count(run.settings["channels_count"]), len(links)))

    async def _bots(self, session, me, full, run):
        self._chance(run, "bots_chance")
        bot = random.choice(self._pool(run, "bots"))
        await self.safe_call(lambda: session.send_message(bot, "/start"))
        await asyncio.sleep(random.randint(*BOT_ANSWER))  # its answer, read as a person would
        await session.send_read_acknowledge(bot)
        return f"/start в {bot}"

    async def _notes(self, session, me, full, run):
        notes = self._pool(run, "notes")
        sent = []
        for index, note in enumerate(random.sample(notes, k=min(random.randint(1, 3), len(notes)))):
            if index:
                await self.delay()
            sent.append(await self.safe_call(lambda: session.send_message("me", note)))
        await session.pin_message("me", random.choice(sent), notify=False)
        return f"заметок в «Избранном»: {len(sent)}, одна закреплена"
