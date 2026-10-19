"""Сервер настоящим клиентом MCP в памяти: схема инструментов и чтение пустого хранилища."""

import asyncio
from datetime import date

from mcp import Client

import eshop_store as es
import server
import steam_store as ss

ИМЕНА = {
    "steam_tags", "steam_search", "steam_find", "steam_game", "eshop_find",
    "list_suggestions", "list_wishlist", "list_calendar", "list_library", "list_feed",
}


def test_десять_инструментов_и_пустое_хранилище(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))

    async def прогон():
        async with Client(server.srv) as к:
            имена = {и.name for и in (await к.list_tools()).tools}
            assert имена == ИМЕНА
            итог = await к.call_tool("list_wishlist", {})
            assert not итог.is_error
            assert итог.structured_content == {"желаемое": []}

    asyncio.run(прогон())
    # Сервер только читает: пустое хранилище он не заводит.
    assert not (tmp_path / "games.sqlite").exists()


def test_eshop_find_отдаёт_записи_каталога(monkeypatch):
    async def подделка(к, название):
        assert название == "hollow knight"
        return [ss.Игра(
            70010000003208, "Hollow Knight",
            адрес="https://www.nintendo.com/us/store/products/hollow-knight-switch/",
            цена=1500, валюта="USD", платформы=("Nintendo Switch",),
            описание="Подземное королевство жуков.", жанры=("Action", "Platformer"),
            дата_текст="Jun 12, 2018", дата=date(2018, 6, 12), вышла=False, скидка=33,
        )]

    monkeypatch.setattr(es, "найти_по_названию", подделка)

    async def прогон():
        async with Client(server.srv) as к:
            return await к.call_tool("eshop_find", {"name": "hollow knight"})

    итог = asyncio.run(прогон())
    assert not итог.is_error
    assert итог.structured_content == {"игры": [{
        "номер": 70010000003208,
        "имя": "Hollow Knight",
        "платформы": ["Nintendo Switch"],
        "выход": "Jun 12, 2018",
        "вышла": False,
        "цена_usd": 15.0,
        "скидка_процентов": 33,
        "жанры": ["Action", "Platformer"],
        "описание": "Подземное королевство жуков.",
        "ссылка": "https://www.nintendo.com/us/store/products/hollow-knight-switch/",
    }]}


def test_eshop_find_сбой_магазина_ошибка_инструмента(monkeypatch):
    async def подделка(к, название):
        raise ss.ОшибкаМагазина("магазин недоступен")

    monkeypatch.setattr(es, "найти_по_названию", подделка)

    async def прогон():
        async with Client(server.srv) as к:
            return await к.call_tool("eshop_find", {"name": "hollow knight"})

    итог = asyncio.run(прогон())
    assert итог.is_error
    assert "магазин недоступен" in итог.content[0].text


def test_игра_словарём_различает_магазины():
    steam = server._игра_словарём(ss.Игра(620, "Portal 2", цена=19900, валюта="RUB"))
    assert steam["магазин"] == "steam"
    assert steam["цена_руб"] == 199.0
    assert "цена_usd" not in steam
    eshop = server._игра_словарём(ss.Игра(70010000003208, "Hollow Knight", цена=1500, валюта="USD"))
    assert eshop["магазин"] == "eshop"
    assert eshop["цена_usd"] == 15.0
    assert "цена_руб" not in eshop
