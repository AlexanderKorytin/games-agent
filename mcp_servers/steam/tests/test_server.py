"""Сервер настоящим клиентом MCP в памяти: схема инструментов и чтение пустого хранилища."""

import asyncio
from datetime import date

from mcp import Client

import pytest

import eshop
import games_db
import server
import steam_store as ss

ИМЕНА = {
    "steam_tags", "steam_search", "steam_find", "steam_game", "eshop_find", "eshop_game",
    "list_suggestions", "list_wishlist", "list_calendar", "list_library", "list_feed",
}


def test_одиннадцать_инструментов_и_пустое_хранилище(tmp_path, monkeypatch):
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


def _страны(tmp_path, monkeypatch, *, steam=None, eshop_=None):
    """Хранилище во временном каталоге со странами магазинов."""
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))
    с = games_db.открыть()
    games_db.задать_страны(с, steam=steam, eshop=eshop_)
    с.close()


def _вызов(инструмент, доводы):
    async def прогон():
        async with Client(server.srv) as к:
            return await к.call_tool(инструмент, доводы)
    return asyncio.run(прогон())


def test_eshop_find_отдаёт_записи_каталога(tmp_path, monkeypatch):
    _страны(tmp_path, monkeypatch, eshop_="MX")

    async def подделка(к, регион, название):
        assert регион == eshop.регион("MX")
        assert название == "hollow knight"
        return [ss.Игра(
            70010000003208, "Hollow Knight",
            адрес="https://www.nintendo.com/us/store/products/hollow-knight-switch/",
            цена=1500, валюта="USD", платформы=("Nintendo Switch",),
            описание="Подземное королевство жуков.", жанры=("Action", "Platformer"),
            дата_текст="Jun 12, 2018", дата=date(2018, 6, 12), вышла=False, скидка=33,
        )]

    monkeypatch.setattr(eshop, "найти_по_названию", подделка)
    итог = _вызов("eshop_find", {"name": "hollow knight"})
    assert not итог.is_error
    assert итог.structured_content == {"игры": [{
        "номер": 70010000003208,
        "имя": "Hollow Knight",
        "платформы": ["Nintendo Switch"],
        "выход": "Jun 12, 2018",
        "вышла": False,
        "цена": 15.0,
        "валюта": "USD",
        "скидка_процентов": 33,
        "жанры": ["Action", "Platformer"],
        "описание": "Подземное королевство жуков.",
        "ссылка": "https://www.nintendo.com/us/store/products/hollow-knight-switch/",
    }]}


def test_eshop_find_сбой_магазина_ошибка_инструмента(tmp_path, monkeypatch):
    _страны(tmp_path, monkeypatch, eshop_="US")

    async def подделка(к, регион, название):
        raise ss.ОшибкаМагазина("магазин недоступен")

    monkeypatch.setattr(eshop, "найти_по_названию", подделка)
    итог = _вызов("eshop_find", {"name": "hollow knight"})
    assert итог.is_error
    assert "магазин недоступен" in итог.content[0].text


def test_игра_словарём_различает_магазины():
    steam = server._игра_словарём(ss.Игра(620, "Portal 2", цена=410000, валюта="KZT"))
    assert steam["магазин"] == "steam"
    assert (steam["цена"], steam["валюта"]) == (4100.0, "KZT")
    е = server._игра_словарём(ss.Игра(70010000003208, "Hollow Knight", цена=1500, валюта="USD"))
    assert е["магазин"] == "eshop"
    assert (е["цена"], е["валюта"]) == (15.0, "USD")


@pytest.mark.parametrize("страна", ["KZ", "DE"])
def test_инструменты_steam_шлют_страну_из_хранилища(tmp_path, monkeypatch, страна):
    _страны(tmp_path, monkeypatch, steam=страна)
    страны: list[tuple[str, str]] = []

    async def найти(к, метка, *, будущие, страница=0, страна):
        страны.append(("найти", страна))
        return [620]

    async def найти_по_названию(к, название, язык="russian", *, страна):
        страны.append(("найти_по_названию", страна))
        return [(620, "Portal 2")]

    async def игра(к, номер, *, с_отзывами=True, страна):
        страны.append(("игра", страна))
        return ss.Игра(номер, "Portal 2", цена=410000, валюта="KZT")

    monkeypatch.setattr(ss, "найти", найти)
    monkeypatch.setattr(ss, "найти_по_названию", найти_по_названию)
    monkeypatch.setattr(ss, "игра", игра)
    assert _вызов("steam_search", {"tag": 122}).structured_content == {"номера": [620]}
    assert not _вызов("steam_find", {"name": "portal"}).is_error
    итог = _вызов("steam_game", {"appid": 620})
    assert (итог.structured_content["цена"], итог.structured_content["валюта"]) == (4100.0, "KZT")
    assert страны == [("найти", страна), ("найти_по_названию", страна), ("игра", страна)]


@pytest.mark.parametrize("хранилище", [False, True])
@pytest.mark.parametrize("инструмент, доводы", [
    ("steam_search", {"tag": 122}), ("steam_find", {"name": "portal"}), ("steam_game", {"appid": 620}),
])
def test_нет_страны_steam_ошибка_инструмента(tmp_path, monkeypatch, хранилище, инструмент, доводы):
    """Нет файла хранилища или страны в нём — понятная ошибка, в магазин запроса нет."""
    if хранилище:
        _страны(tmp_path, monkeypatch, eshop_="US")
    else:
        monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))

    async def не_звать(*доводы, **поля):
        raise AssertionError("без страны магазин не зовётся")

    for имя in ("найти", "найти_по_названию", "игра"):
        monkeypatch.setattr(ss, имя, не_звать)
    итог = _вызов(инструмент, доводы)
    assert итог.is_error
    assert "страна Steam не выбрана — укажите в настройках агента" in итог.content[0].text
    assert хранилище or not (tmp_path / "games.sqlite").exists()


@pytest.mark.parametrize("страна, текст", [
    (None, "страна eShop не выбрана — укажите в настройках агента"),
    ("RU", "eShop страны RU не поддержан"),
])
def test_нет_страны_eshop_ошибка_инструмента(tmp_path, monkeypatch, страна, текст):
    _страны(tmp_path, monkeypatch, steam="KZ", eshop_=страна)

    async def не_звать(*доводы, **поля):
        raise AssertionError("без страны eShop не зовётся")

    monkeypatch.setattr(eshop, "найти_по_названию", не_звать)
    итог = _вызов("eshop_find", {"name": "hollow knight"})
    assert итог.is_error
    assert текст in итог.content[0].text


def test_тексты_для_модели_без_зашитой_страны():
    """Инструкция сервера и описания инструментов не называют страну: магазин — страны человека."""
    async def прогон():
        async with Client(server.srv) as к:
            return await к.list_tools()

    тексты = [server.srv.instructions or ""] + [и.description or "" for и in asyncio.run(прогон()).tools]
    for текст in тексты:
        assert "США" not in текст and "рубл" not in текст and "доллар" not in текст, текст
    eshop_find = next(и for и in asyncio.run(прогон()).tools if и.name == "eshop_find")
    assert "eShop страны пользователя" in eshop_find.description


НОМЕР_ESHOP = 70010000003208


def _игра_eshop(tmp_path, monkeypatch, *, страна="MX", объект="70010000003208", включён=True,
                отзывы=ss.НЕТ_ОТЗЫВОВ):
    """Хранилище со страной eShop и записью игры eShop."""
    _страны(tmp_path, monkeypatch, eshop_=страна)
    с = games_db.открыть()
    games_db.запомнить_игру(с, ss.Игра(НОМЕР_ESHOP, "Hollow Knight", объект=объект, отзывы=отзывы))
    games_db.включить_eshop(с, включён)
    с.close()


def _не_звать_eshop(monkeypatch):
    async def не_звать(*доводы, **поля):
        raise AssertionError("eShop не зовётся")

    monkeypatch.setattr(eshop, "игра", не_звать)


def test_eshop_game_отдаёт_сведения(tmp_path, monkeypatch):
    _игра_eshop(tmp_path, monkeypatch)

    async def подделка(к, регион, объект):
        assert регион == eshop.регион("MX")
        assert объект == "70010000003208"
        return ss.Игра(
            НОМЕР_ESHOP, "Hollow Knight",
            адрес="https://www.nintendo.com/us/store/products/hollow-knight-switch/",
            цена=1500, валюта="USD", платформы=("Nintendo Switch",),
            описание="Подземное королевство жуков.", жанры=("Action", "Platformer"),
            дата_текст="Jun 12, 2018", дата=date(2018, 6, 12), вышла=True, скидка=33,
        )

    monkeypatch.setattr(eshop, "игра", подделка)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert not итог.is_error
    ожидание = server._игра_словарём(ss.Игра(
        НОМЕР_ESHOP, "Hollow Knight",
        адрес="https://www.nintendo.com/us/store/products/hollow-knight-switch/",
        цена=1500, валюта="USD", описание="Подземное королевство жуков.",
        жанры=("Action", "Platformer"), дата_текст="Jun 12, 2018", дата=date(2018, 6, 12),
        вышла=True, скидка=33,
    ))
    assert итог.structured_content == {
        **ожидание, "платформы": ["Nintendo Switch"],
        "оценка_пользователей": "игроки Metacritic: отзывов нет",
    }
    assert итог.structured_content["магазин"] == "eshop"
    assert итог.structured_content["цена"] == 15.0


@pytest.mark.parametrize("свежие, ожидание", [
    (ss.НЕТ_ОТЗЫВОВ, "игроки Metacritic: Очень положительные, 90 % из 1 000"),
    (ss.Отзывы("Положительные", 70, 100), "игроки Metacritic: Положительные, 70 % из 100"),
])
def test_eshop_game_оценка_игроков_metacritic(tmp_path, monkeypatch, свежие, ожидание):
    """Свежая запись каталога без оценки — оценка из хранилища; с оценкой — её."""
    _игра_eshop(tmp_path, monkeypatch, отзывы=ss.Отзывы("Очень положительные", 900, 1000))

    async def подделка(к, регион, объект):
        return ss.Игра(НОМЕР_ESHOP, "Hollow Knight", отзывы=свежие)

    monkeypatch.setattr(eshop, "игра", подделка)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert not итог.is_error
    assert итог.structured_content["оценка_пользователей"] == ожидание


@pytest.mark.parametrize("хранилище", [False, True])
def test_eshop_game_выключен_раньше_неизвестной(tmp_path, monkeypatch, хранилище):
    """eShop выключен и игры нет в хранилище (или нет и файла) — ответ «выключен»."""
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))
    if хранилище:
        с = games_db.открыть()
        games_db.включить_eshop(с, False)
        с.close()
    _не_звать_eshop(monkeypatch)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert итог.is_error
    текст = итог.content[0].text
    if хранилище:
        assert "eShop выключен в настройках агента" in текст
    else:
        # Без файла хранилища выключателя нет: eShop включён, игра агенту неизвестна.
        assert f"игра eShop {НОМЕР_ESHOP} неизвестна агенту" in текст


def test_steam_game_номер_eshop_ошибка(tmp_path, monkeypatch):
    _страны(tmp_path, monkeypatch, steam="KZ")

    async def не_звать(*доводы, **поля):
        raise AssertionError("номер eShop в Steam не шлётся")

    monkeypatch.setattr(ss, "игра", не_звать)
    итог = _вызов("steam_game", {"appid": НОМЕР_ESHOP})
    assert итог.is_error
    assert f"номер {НОМЕР_ESHOP} — eShop: сведения — eshop_game" in итог.content[0].text


def test_eshop_game_номер_steam_ошибка(tmp_path, monkeypatch):
    _игра_eshop(tmp_path, monkeypatch)
    _не_звать_eshop(monkeypatch)
    итог = _вызов("eshop_game", {"nsuid": 620})
    assert итог.is_error
    assert "620" in итог.content[0].text and "steam_game" in итог.content[0].text


@pytest.mark.parametrize("хранилище", [False, True])
def test_eshop_game_неизвестна_агенту(tmp_path, monkeypatch, хранилище):
    if хранилище:
        _страны(tmp_path, monkeypatch, eshop_="MX")
    else:
        monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))
    _не_звать_eshop(monkeypatch)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert итог.is_error
    assert f"игра eShop {НОМЕР_ESHOP} неизвестна агенту" in итог.content[0].text
    assert хранилище or not (tmp_path / "games.sqlite").exists()


def test_eshop_game_без_объекта_ошибка(tmp_path, monkeypatch):
    _игра_eshop(tmp_path, monkeypatch, объект="")
    _не_звать_eshop(monkeypatch)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert итог.is_error
    assert f"у игры eShop {НОМЕР_ESHOP} нет записи каталога" in итог.content[0].text


@pytest.mark.parametrize("страна, текст", [
    (None, "страна eShop не выбрана — укажите в настройках агента"),
    ("RU", "eShop страны RU не поддержан"),
])
def test_eshop_game_нет_страны_ошибка(tmp_path, monkeypatch, страна, текст):
    _игра_eshop(tmp_path, monkeypatch, страна=страна)
    _не_звать_eshop(monkeypatch)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert итог.is_error
    assert текст in итог.content[0].text


def test_eshop_game_eshop_выключен_ошибка(tmp_path, monkeypatch):
    _игра_eshop(tmp_path, monkeypatch, включён=False)
    _не_звать_eshop(monkeypatch)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert итог.is_error
    assert "eShop выключен в настройках агента" in итог.content[0].text


def test_eshop_game_нет_в_каталоге_страны(tmp_path, monkeypatch):
    _игра_eshop(tmp_path, monkeypatch)

    async def подделка(к, регион, объект):
        return None

    monkeypatch.setattr(eshop, "игра", подделка)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert итог.is_error
    assert f"игры eShop {НОМЕР_ESHOP} нет в каталоге eShop страны MX" in итог.content[0].text


def test_eshop_game_сбой_магазина_ошибка(tmp_path, monkeypatch):
    _игра_eshop(tmp_path, monkeypatch)

    async def подделка(к, регион, объект):
        raise ss.ОшибкаМагазина("магазин недоступен")

    monkeypatch.setattr(eshop, "игра", подделка)
    итог = _вызов("eshop_game", {"nsuid": НОМЕР_ESHOP})
    assert итог.is_error
    assert "магазин недоступен" in итог.content[0].text


def test_eshop_game_описание():
    async def прогон():
        async with Client(server.srv) as к:
            return await к.list_tools()

    инструмент = next(и for и in asyncio.run(прогон()).tools if и.name == "eshop_game")
    assert " ".join((инструмент.description or "").split()) == (
        "Сведения об игре Nintendo eShop по номеру (eShop страны пользователя): жанры, дата, "
        "цена, описание, оценка игроков Metacritic, если агент её знает. Номер — eShop, "
        "не Steam: Steam — steam_game."
    )
    assert "eshop_game" in (server.srv.instructions or "")
