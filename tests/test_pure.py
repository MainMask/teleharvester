"""Pure-function tests (no network / telethon calls). Run from the repo root:

    python -m unittest discover -s tests -p 'test_*.py'
"""
import dataclasses
import unittest

from functions.base.base import BaseFunction
from functions.inviting import InvitingFunc
from functions.changename import ChangeNameFunc
from modules.types.proxy import Proxy
from modules.types.account_settings import AccountSettings
from modules.generators.telegram_android import TelegramAppAPI
from modules.generators.linux import LinuxAPI


class ParseDelayTest(unittest.TestCase):
    def test_range(self):
        self.assertEqual(BaseFunction().parse_delay("3-7"), [3, 7])

    def test_single(self):
        self.assertEqual(BaseFunction().parse_delay("5"), [5])


class ParseMessageLinkTest(unittest.TestCase):
    def test_public(self):
        self.assertEqual(
            BaseFunction.parse_message_link("https://t.me/durov/123"),
            ("durov", 123),
        )

    def test_private(self):
        peer, message_id = BaseFunction.parse_message_link("https://t.me/c/1234567890/89")
        self.assertEqual(peer.channel_id, 1234567890)
        self.assertEqual(message_id, 89)


class ChunkifyTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(InvitingFunc.chunkify([1, 2, 3, 4, 5], 2), [[1, 3, 5], [2, 4]])


class InviteLinkTest(unittest.TestCase):
    def test_is_public(self):
        self.assertTrue(InvitingFunc.is_public("@durov"))
        self.assertTrue(InvitingFunc.is_public("https://t.me/durov"))
        self.assertFalse(InvitingFunc.is_public("https://t.me/+AbCdEf"))
        self.assertFalse(InvitingFunc.is_public("https://t.me/joinchat/AbCdEf"))
        self.assertFalse(InvitingFunc.is_public("+AbCdEf"))

    def test_invite_hash(self):
        self.assertEqual(InvitingFunc.invite_hash("https://t.me/+AbCdEf"), "AbCdEf")
        self.assertEqual(InvitingFunc.invite_hash("https://t.me/joinchat/AbCdEf"), "AbCdEf")

    def test_public_ref(self):
        self.assertEqual(InvitingFunc.public_ref("https://t.me/durov"), "@durov")
        self.assertEqual(InvitingFunc.public_ref("@durov"), "@durov")
        self.assertEqual(InvitingFunc.public_ref("durov"), "@durov")


class GetRandomNameTest(unittest.TestCase):
    def test_single_token(self):
        self.assertEqual(ChangeNameFunc.get_random_name(["John"]), ("John", None))

    def test_multi_token(self):
        self.assertEqual(
            ChangeNameFunc.get_random_name(["John Doe Smith"]),
            ("John", "Doe Smith"),
        )


class ProxyTest(unittest.TestCase):
    def test_with_credentials(self):
        proxy = Proxy("socks5", "1.2.3.4", 1080, "user", "pass")
        self.assertEqual(
            proxy.as_telethon(),
            ("socks5", "1.2.3.4", 1080, False, "user", "pass"),
        )

    def test_without_credentials(self):
        self.assertEqual(
            Proxy("socks5", "1.2.3.4", 1080).as_telethon(),
            ("socks5", "1.2.3.4", 1080),
        )

    def test_blank_credentials(self):
        self.assertEqual(
            Proxy("socks5", "1.2.3.4", 1080, "", "").as_telethon(),
            ("socks5", "1.2.3.4", 1080),
        )


class AccountSettingsRoundTripTest(unittest.TestCase):
    def _base_dict(self, proxy):
        return {
            "auth_key": "KEY",
            "account": {
                "first_name": "John",
                "last_name": "Doe",
                "user_id": 123,
                "added_at": 1700000000.0,
                "phone_number": "123456789",
            },
            "application": {
                "api_id": 6,
                "api_hash": "hash",
                "device_name": "PC 64bit",
                "app_version": "4.0.2",
                "sdk": "Linux",
                "lang_pack": "tdesktop",
                "system_lang_code": "en",
            },
            "proxy": proxy,
        }

    def test_roundtrip_no_proxy(self):
        data = self._base_dict(None)
        restored = dataclasses.asdict(AccountSettings.from_dict(data))
        self.assertEqual(restored, data)

    def test_roundtrip_with_proxy(self):
        data = self._base_dict({
            "proxy_type": "socks5",
            "ip": "1.2.3.4",
            "port": 1080,
            "user": "u",
            "password": "p",
        })
        restored = dataclasses.asdict(AccountSettings.from_dict(data))
        self.assertEqual(restored, data)


class GeneratorsTest(unittest.TestCase):
    def test_android(self):
        self.assertIn(TelegramAppAPI.device(), TelegramAppAPI.device_models)
        self.assertIn(TelegramAppAPI.app_version(), TelegramAppAPI.app_versions)
        self.assertIn(TelegramAppAPI.sdk(), TelegramAppAPI.sdk_versions)

    def test_linux(self):
        self.assertEqual(LinuxAPI.device(), "PC 64bit")
        self.assertIn(LinuxAPI.app_version(), LinuxAPI.app_versions)
        self.assertTrue(LinuxAPI.sdk().startswith("Linux "))

    def test_system_lang_code(self):
        allowed = {"zh-hans", "cn", "en", "ru", "af", "sq", "cs", "pl"}
        self.assertIn(TelegramAppAPI.system_lang_code(), allowed)


if __name__ == "__main__":
    unittest.main()
