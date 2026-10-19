"""Metacritic: разбор образцов, снятых 2026-10-19 настоящими запросами, и ход запросов на
`httpx2.MockTransport`."""

import asyncio
import json
from pathlib import Path

import httpx2
import pytest

import metacritic as mc
import steam_store as ss

ОБРАЗЦЫ = Path(__file__).parent.parent / "образцы"


def образец(имя: str):
    return json.loads((ОБРАЗЦЫ / имя).read_text("utf-8"))


def test_поиск_своя_запись():
    выдача = образец("metacritic_search_bananza.json")
    assert mc.разобрать_поиск(выдача, "Donkey Kong Bananza") == ("donkey-kong-bananza", ("Nintendo Switch 2",))
    # Имя eShop со знаком марки сравнивается так же.
    assert mc.разобрать_поиск(выдача, "Donkey Kong Bananza™") == ("donkey-kong-bananza", ("Nintendo Switch 2",))
    # Дополнение стоит в выдаче вторым: берётся своя запись, а не первая.
    assert mc.разобрать_поиск(выдача, "Donkey Kong Bananza: DK Island & Emerald Rush") == (
        "donkey-kong-bananza-dk-island-and-emerald-rush", ("Nintendo Switch 2",)
    )
    assert mc.разобрать_поиск(выдача, "Donkey Kong Country: Tropical Freeze") == (
        "donkey-kong-country-tropical-freeze", ("Wii U", "Nintendo Switch")
    )


def test_поиск_без_совпадения():
    выдача = образец("metacritic_search_bananza.json")
    assert mc.разобрать_поиск(выдача, "Donkey Kong") is None
    assert mc.разобрать_поиск(выдача, "™") is None
    assert mc.разобрать_поиск({"data": {"items": []}}, "Donkey Kong Bananza") is None


def test_поиск_не_тот_ответ():
    with pytest.raises(ss.ОшибкаМагазина):
        mc.разобрать_поиск({"error": "x"}, "Donkey Kong Bananza")


def test_платформа():
    все = ("PC", "Nintendo Switch", "Nintendo Switch 2")
    assert mc.платформа(все, switch2=True) == "nintendo-switch-2"
    assert mc.платформа(все, switch2=False) == "nintendo-switch"
    assert mc.платформа(("PC",), True) is None
    # Своей платформы у записи нет — берётся другая платформа Switch.
    assert mc.платформа(("Nintendo Switch 2",), switch2=False) == "nintendo-switch-2"
    assert mc.платформа(("Wii U", "Nintendo Switch"), switch2=True) == "nintendo-switch"


def test_оценка_игроков():
    assert mc.разобрать_оценку(образец("metacritic_user_bananza.json")) == ss.Отзывы("в целом хорошие", 2848, 3293)
    assert mc.разобрать_оценку({"data": {"item": {"reviewCount": 0, "positiveCount": 0}}}) == ss.НЕТ_ОТЗЫВОВ
    # Сводка вне словаря — без сводки, а не английским словом на русской странице.
    assert mc.разобрать_оценку(
        {"data": {"item": {"reviewCount": 10, "positiveCount": 7, "sentiment": "Something new"}}}
    ) == ss.Отзывы("", 7, 10)


def test_оценка_не_тот_ответ():
    with pytest.raises(ss.ОшибкаМагазина):
        mc.разобрать_оценку({"data": None})


def _прогнать(отвечать, игра):
    async def прогон():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(отвечать)) as к:
            return await mc.отзывы(к, игра)

    return asyncio.run(прогон())


def test_отзывы_поиск_и_оценка():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        if запрос.url.path.startswith("/finder/"):
            return httpx2.Response(200, json=образец("metacritic_search_bananza.json"))
        return httpx2.Response(200, json=образец("metacritic_user_bananza.json"))

    игра = ss.Игра(70010000095770, "Donkey Kong Bananza™", платформы=("Nintendo Switch 2",))
    assert _прогнать(отвечать, игра) == ss.Отзывы("в целом хорошие", 2848, 3293)
    поиск, оценка = запросы
    # Поиск — по чистому имени, без знака марки.
    assert поиск.url.host == "backend.metacritic.com"
    assert поиск.url.path == "/finder/metacritic/search/Donkey Kong Bananza/web"
    assert dict(поиск.url.params) == {"mcoTypeId": "13"}
    assert str(оценка.url) == (
        "https://backend.metacritic.com/reviews/metacritic/user/games/donkey-kong-bananza/stats/web"
        "?platform=nintendo-switch-2"
    )


def test_отзывы_имя_с_знаками_адреса():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        if запрос.url.path.startswith("/finder/"):
            return httpx2.Response(200, json=образец("metacritic_search_bananza.json"))
        return httpx2.Response(200, json=образец("metacritic_user_bananza.json"))

    игра = ss.Игра(70010000100000, "Donkey Kong Bananza: DK Island & Emerald Rush",
                   платформы=("Nintendo Switch 2",))
    _прогнать(отвечать, игра)
    # «&» и «:» в имени не рвут адрес: строка поиска — один кусок пути.
    assert запросы[0].url.raw_path.startswith(b"/finder/metacritic/search/Donkey%20Kong%20Bananza%3A%20DK%20Island%20%26%20")
    assert запросы[1].url.path == (
        "/reviews/metacritic/user/games/donkey-kong-bananza-dk-island-and-emerald-rush/stats/web"
    )


def test_отзывы_без_совпадения_одним_запросом():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("metacritic_search_bananza.json"))

    игра = ss.Игра(70010000000001, "Donkey Kong", платформы=("Nintendo Switch",))
    assert _прогнать(отвечать, игра) == ss.НЕТ_ОТЗЫВОВ
    assert len(запросы) == 1


def test_отзывы_без_платформы_switch_одним_запросом():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("metacritic_search_bananza.json"))

    игра = ss.Игра(70010000000002, "Donkey Kong 64", платформы=("Nintendo Switch",))
    assert _прогнать(отвечать, игра) == ss.НЕТ_ОТЗЫВОВ
    assert len(запросы) == 1


def test_отзывы_страницы_оценки_нет():
    def отвечать(запрос):
        if запрос.url.path.startswith("/finder/"):
            return httpx2.Response(200, json=образец("metacritic_search_bananza.json"))
        return httpx2.Response(404)

    игра = ss.Игра(70010000095770, "Donkey Kong Bananza", платформы=("Nintendo Switch 2",))
    assert _прогнать(отвечать, игра) == ss.НЕТ_ОТЗЫВОВ


def test_клиент():
    к = mc.клиент()
    assert к.headers["User-Agent"] == ss.ПРЕДСТАВЛЕНИЕ
    asyncio.run(к.aclose())


def test_отзывы_платформа_по_игре():
    # Синтетическая выдача: у записи обе платформы Switch, выбор решает платформа игры.
    выдача = {"data": {"items": [{"title": "Two Platforms", "slug": "dve-platformy", "platforms": [
        {"id": None, "name": "Nintendo Switch"}, {"id": None, "name": "Nintendo Switch 2"}]}]}}

    def платформа_запроса(платформы):
        запросы = []

        def отвечать(запрос):
            запросы.append(запрос)
            if запрос.url.path.startswith("/finder/"):
                return httpx2.Response(200, json=выдача)
            return httpx2.Response(200, json=образец("metacritic_user_bananza.json"))

        _прогнать(отвечать, ss.Игра(70010000000003, "Two Platforms", платформы=платформы))
        return запросы[1].url.params["platform"]

    assert платформа_запроса(("Nintendo Switch",)) == "nintendo-switch"
    assert платформа_запроса(("Nintendo Switch 2",)) == "nintendo-switch-2"


def test_отзывы_поиск_404_ошибка():
    def отвечать(запрос):
        return httpx2.Response(404)

    игра = ss.Игра(70010000095770, "Donkey Kong Bananza", платформы=("Nintendo Switch 2",))
    with pytest.raises(ss.ОшибкаМагазина):
        _прогнать(отвечать, игра)
