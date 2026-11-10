"""Text files made on Windows (Notepad's «UTF-8 with BOM») read as without it: an invisible U+FEFF must not land
on the first line — a proxy list refused whole, a list's first recipient dropped, a wrong 2FA password, a seller's
.json read as none."""
import asyncio
import io
import json
import zipfile
from types import SimpleNamespace as ns

from bot.routers import _common
from functions.pmmailing import PmMailingFunc
from modules import content, session_import, tdata_import

BOM = "﻿"


def _write(path, text):
    path.write_bytes(text.encode("utf-8-sig"))
    return path


def test_a_proxies_file(tmp_path):
    proxies = tdata_import.load_proxies(str(_write(tmp_path / "proxies.txt", "socks5://1.2.3.4:1080\n")))
    assert [proxy.ip for proxy in proxies] == ["1.2.3.4"]


def test_a_2fa_txt_next_to_a_tdata_and_in_its_zip(tmp_path):
    _write(tmp_path / "2fa.txt", "mypass\n")
    assert tdata_import.read_2fa_password(str(tmp_path)) == "mypass"

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("acc/2fa.txt", ("2fa: " + "mypass").encode("utf-8-sig"))
    assert tdata_import.zip_2fa_password(archive.getvalue()) == "mypass"


def test_a_seller_s_session_json(tmp_path):
    (tmp_path / "acc.session").write_bytes(b"")
    _write(tmp_path / "acc.json", json.dumps({"twoFA": "pass", "app_id": 6}))
    (path, meta), = session_import.collect(str(tmp_path))
    assert meta == {"twoFA": "pass", "app_id": 6}


def test_a_region_s_list(tmp_path, monkeypatch):
    monkeypatch.setattr(content, "DIR", str(tmp_path))
    (tmp_path / "ru").mkdir()
    _write(tmp_path / "ru" / "names.txt", "Иван Петров\nОльга\n")
    assert content.pool("ru", "names") == ["Иван Петров", "Ольга"]


def test_a_recipients_file_on_the_server(tmp_path):
    path = _write(tmp_path / "targets.txt", "@alice\n+79991234567\n")
    recipients = PmMailingFunc.load_recipients(ns(parse_list=PmMailingFunc.parse_list), str(path))
    assert recipients == ["@alice", "+79991234567"]


def test_a_txt_sent_to_the_bot(monkeypatch):
    async def download(message, document):
        return (BOM + "@alice\n@bob").encode("utf-8")
    monkeypatch.setattr(_common, "download", download)
    message = ns(document=ns(file_size=10))
    assert asyncio.run(_common.read_text_document(message, 100)) == "@alice\n@bob"
