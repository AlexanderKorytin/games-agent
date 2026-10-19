"""Nintendo eShop: разбор образцов, снятых 2026-10-19 настоящими запросами, и ход запросов на
`httpx2.MockTransport`."""

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx2
import pytest

import eshop_store as es
import steam_store as ss

ОБРАЗЦЫ = Path(__file__).parent.parent / "образцы"
США = es.регион("US")
МЕКСИКА = es.регион("MX")


def образец(имя: str):
    return json.loads((ОБРАЗЦЫ / имя).read_text("utf-8"))


def test_регионы():
    assert США == es.Регион("US", "америки", "store_game_en_us")
    assert МЕКСИКА == es.Регион("MX", "америки", "store_game_es_mx")
    assert es.регион("DE") == es.Регион("DE", "европа", "de")
    assert es.регион("PL") == es.Регион("PL", "европа", "en")
    assert es.регион("ZA") == es.Регион("ZA", "европа", "za")
    for страна in ("RU", "KZ", "JP", "HK", "us", ""):
        assert es.регион(страна) is None
    америки = {"US": "en_us", "CA": "en_ca", "MX": "es_mx", "BR": "pt_br", "AR": "es_ar",
               "CL": "es_cl", "CO": "es_co", "PE": "es_pe"}
    европа = {**dict.fromkeys(("GB", "IE", "MT", "CY", "AU", "NZ"), "en"),
              **dict.fromkeys(("DE", "AT", "CH"), "de"), **dict.fromkeys(("FR", "BE", "LU"), "fr"),
              "IT": "it", "ES": "es", "NL": "nl", "PT": "pt", "ZA": "za",
              **dict.fromkeys(("FI", "GR", "HU", "RO", "BG", "PL", "CZ", "SE", "NO", "DK", "SK",
                               "SI", "HR", "LT", "LV", "EE"), "en")}
    assert es.СТРАНЫ == {
        **{с: es.Регион(с, "америки", "store_game_" + к) for с, к in америки.items()},
        **{с: es.Регион(с, "европа", я) for с, я in европа.items()},
    }
    # У каждой страны Америк есть валюта.
    assert es.ВАЛЮТЫ == {"US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL", "AR": "ARS",
                         "CL": "CLP", "CO": "COP", "PE": "PEN"}


def test_номер_eshop():
    assert es.это_eshop(70010000130792)
    assert not es.это_eshop(1086940)


def test_сравнимое_имя():
    assert es.сравнимое("Hollow Knight – Nintendo Switch 2 Edition") == es.сравнимое("Hollow Knight")
    assert es.сравнимое("Pokémon™ Legends: Arceus") == "pokemon legends arceus"
    assert es.сравнимое("Super Mario Galaxy™ + Super Mario Galaxy™ 2") != es.сравнимое("Super Mario Galaxy™ 2")
    assert (
        es.чистое_имя("The Legend of Zelda™: Tears of the Kingdom – Nintendo Switch™ 2 Edition")
        == "The Legend of Zelda: Tears of the Kingdom"
    )
    # Переведённые хвосты издания Switch 2 индексов Америк срезаются так же.
    for имя in ("Red Dead Redemption: Edição Nintendo Switch™ 2",
                "Red Dead Redemption – Edición Nintendo Switch 2",
                "Red Dead Redemption Édition Nintendo Switch™ 2",
                "Red Dead Redemption: Nintendo Switch 2 Edition"):
        assert es.чистое_имя(имя) == "Red Dead Redemption", имя
    # Издание игры, а не платформы, остаётся.
    assert es.чистое_имя("Lies of P: Edição Completa") == "Lies of P: Edição Completa"


def test_выдача_каталога():
    игры = es.разобрать_выдачу(образец("eshop_upcoming.json"), США)
    # Дополнение (`dlcType`) и пакет обновления (`isUpgrade`) выброшены.
    assert [и.номер for и in игры] == [70010000125145, 70010000135065]
    спорт, кирби = игры
    assert спорт.имя == "Nintendo Switch Sports Resort™"
    assert спорт.дата == date(2026, 10, 22) and спорт.дата_текст == "2026-10-22"
    assert спорт.вышла is False
    assert спорт.цена == 4999 and спорт.цена_без_скидки == 4999 and спорт.скидка == 0
    assert спорт.валюта == "USD" and not спорт.бесплатна
    assert спорт.адрес == "https://www.nintendo.com/us/store/products/nintendo-switch-sports-resort-switch-2/"
    assert спорт.обложка.startswith(es.ОБЛОЖКИ) and спорт.обложка != es.ОБЛОЖКИ
    assert спорт.объект == "7100125145"
    assert спорт.платформы == ("Nintendo Switch 2",)
    assert спорт.жанры == ("Action", "Party", "Sports")
    assert спорт.описание.startswith("Get moving")
    assert кирби.дата is None and кирби.дата_текст == "2027"
    assert кирби.цена is None and кирби.цена_без_скидки is None and not кирби.бесплатна
    assert кирби.описание == ""


def test_выдача_мексики():
    # Образец индекса `store_game_es_mx`: метки и жанры по-испански, цена в песо, адрес `/es-mx/`.
    игры = es.разобрать_выдачу(образец("eshop_upcoming_es_mx.json"), МЕКСИКА)
    # Дополнение «Oleada 3» и «Paquete de mejora» выброшены.
    assert [и.номер for и in игры] == [70010000125145, 70010000135065]
    спорт, кирби = игры
    assert спорт.вышла is False and кирби.вышла is False
    assert спорт.цена == 102900 and спорт.валюта == "MXN"
    assert спорт.жанры == ("Acción", "Deportes", "Fiesta")
    assert спорт.адрес == "https://www.nintendo.com/es-mx/store/products/nintendo-switch-sports-resort-switch-2/"
    assert кирби.дата is None and кирби.дата_текст == "2027" and кирби.валюта == ""
    вышла = {"nsuid": "70010000000001", "title": "x", "availability": ["Disponible ya"]}
    assert es.разобрать_запись(вышла, МЕКСИКА).вышла is True
    assert es.разобрать_запись({**вышла, "availability": ["Reserva"]}, МЕКСИКА).вышла is False
    # Пакет обновления узнаётся и по метке на языке индекса.
    пакет = {**вышла, "isUpgrade": False, "topLevelFilters": ["Pase de mejora"]}
    assert es.разобрать_запись(пакет, МЕКСИКА) is None
    бразилия = es.регион("BR")
    assert es.разобрать_запись({**вышла, "availability": ["Em breve"]}, бразилия).вышла is False
    assert es.разобрать_запись({**пакет, "topLevelFilters": ["Pacote de melhoria"]}, бразилия) is None


def test_метки_по_языку_индекса():
    # Значения — замер фасетов индексов 2026-10-19.
    английские = es.Метки("Coming soon", "Pre-order", "Available now", "Upgrade pack")
    испанские = es.Метки("Próximos lanzamientos", "Reserva", "Disponible ya", "Pase de mejora")
    португальские = es.Метки("Em breve", "Reserve", "Já disponivel", "Pacote de melhoria")
    for страна in ("US", "CA"):
        assert es.метки(es.регион(страна)) == английские
    for страна in ("MX", "AR", "CL", "CO", "PE"):
        assert es.метки(es.регион(страна)) == испанские
    assert es.метки(es.регион("BR")) == португальские


def test_запись_по_коду():
    игра = es.разобрать_запись(образец("eshop_object_70010000130792.json"), США)
    assert игра is not None
    assert игра.номер == 70010000130792 and игра.цена == 5999
    assert игра.адрес.startswith("https://www.nintendo.com/us/store/products/")
    assert es.разобрать_запись({"nsuid": None, "title": "x"}, США) is None
    assert es.разобрать_запись("не запись", США) is None


def test_выдача_без_hits_ошибка():
    with pytest.raises(ss.ОшибкаМагазина):
        es.разобрать_выдачу({"message": "Invalid Application-ID or API key"}, США)


def test_цены_api_ec():
    цены = es.разобрать_цены(образец("eshop_price_us.json"))
    # Номер `not_found` в словаре отсутствует.
    assert цены == {
        70010000000185: es.Цена(цена=1999, цена_без_скидки=3999, скидка=50, валюта="USD", до="2026-11-05")
    }
    без_скидки = {"prices": [{"title_id": 7, "sales_status": "onsale",
                              "regular_price": {"currency": "USD", "raw_value": "9.99"}}]}
    assert es.разобрать_цены(без_скидки) == {7: es.Цена(999, 999, 0, "USD", "")}


def test_цены_ошибка():
    with pytest.raises(ss.ОшибкаМагазина, match="Over ids limit number"):
        es.разобрать_цены({"error": {"code": "4004", "message": "Over ids limit number"}})
    with pytest.raises(ss.ОшибкаМагазина):
        es.разобрать_цены({"country": "US"})


def test_полное_описание_страницы():
    текст = es.разобрать_описание((ОБРАЗЦЫ / "eshop_product.html").read_text("utf-8"))
    assert текст.startswith("Gently swing your racket")
    assert len(текст) > 1000
    assert "<" not in текст
    assert es.разобрать_описание("<html><body>нет данных</body></html>") == ""


def _прогнать(отвечать, работа):
    async def прогон():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(отвечать)) as к:
            return await работа(к)

    return asyncio.run(прогон())


def test_витрина_доводы():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("eshop_upcoming.json"))

    async def работа(к):
        игры = await es.витрина(к, США, будущие=True, страница=2)
        await es.витрина(к, США, будущие=False, страница=0)
        return игры

    assert len(_прогнать(отвечать, работа)) == 2
    будущие, вышедшие = запросы
    assert будущие.method == "POST"
    assert str(будущие.url) == es.ALGOLIA.lower() + "/store_game_en_us/query"
    assert будущие.headers["X-Algolia-Application-Id"] == "U3B6GR4UA3"
    assert будущие.headers["X-Algolia-API-Key"] == es.ЗАГОЛОВКИ_КАТАЛОГА["X-Algolia-API-Key"]
    тело = json.loads(будущие.content)
    assert тело["page"] == 2 and тело["hitsPerPage"] == 50
    # Код категории не переводится, в отличие от `topLevelCategory` («Juegos», «Jogos»).
    assert тело["filters"] == 'topLevelCategoryCode:GAMES AND (availability:"Coming soon" OR availability:"Pre-order")'
    тело = json.loads(вышедшие.content)
    assert тело["page"] == 0
    assert тело["filters"] == 'topLevelCategoryCode:GAMES AND availability:"Available now"'


def test_витрина_мексики():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("eshop_upcoming_es_mx.json"))

    async def работа(к):
        игры = await es.витрина(к, МЕКСИКА, будущие=True, страница=0)
        await es.витрина(к, МЕКСИКА, будущие=False, страница=0)
        return игры

    игры = _прогнать(отвечать, работа)
    assert [и.валюта for и in игры] == ["MXN", ""]
    будущие, вышедшие = запросы
    assert str(будущие.url) == es.ALGOLIA.lower() + "/store_game_es_mx/query"
    assert json.loads(будущие.content)["filters"] == (
        'topLevelCategoryCode:GAMES AND (availability:"Próximos lanzamientos" OR availability:"Reserva")'
    )
    assert json.loads(вышедшие.content)["filters"] == 'topLevelCategoryCode:GAMES AND availability:"Disponible ya"'


def test_найти_по_названию_eshop():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("eshop_find_hollow_knight.json"))

    игры = _прогнать(отвечать, lambda к: es.найти_по_названию(к, США, "Hollow Knight"))
    # Два пакета обновления выброшены.
    assert len(игры) == 5 and игры[0].номер == 70010000003208
    тело = json.loads(запросы[0].content)
    assert тело["query"] == "Hollow Knight" and тело["hitsPerPage"] == 10


def test_цены_частями():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        номера = запрос.url.params["ids"].split(",")
        return httpx2.Response(200, json={"prices": [
            {"title_id": int(н), "sales_status": "onsale",
             "regular_price": {"currency": "USD", "raw_value": "1.00"}}
            for н in номера
        ]})

    номера = list(range(70010000000000, 70010000000120))
    цены = _прогнать(отвечать, lambda к: es.цены(к, США, номера))
    assert [len(з.url.params["ids"].split(",")) for з in запросы] == [50, 50, 20]
    assert set(цены) == set(номера)
    assert all(dict(з.url.params)["country"] == "US" for з in запросы)
    assert _прогнать(отвечать, lambda к: es.цены(к, США, [])) == {} and len(запросы) == 3
    запросы.clear()
    _прогнать(отвечать, lambda к: es.цены(к, МЕКСИКА, номера[:3]))
    assert dict(запросы[0].url.params)["country"] == "MX"


def test_запись_нет_в_каталоге():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(404, json={"message": "ObjectID does not exist", "status": 404})

    assert _прогнать(отвечать, lambda к: es.запись(к, МЕКСИКА, "0000000000")) is None
    # Хозяина адреса `httpx2` приводит к нижнему регистру.
    assert str(запросы[0].url) == es.ALGOLIA.lower() + "/store_game_es_mx/0000000000"


def test_запись_и_полное_описание():
    def отвечать(запрос):
        if запрос.url.host == "www.nintendo.com":
            assert запрос.url.path == "/us/store/products/nintendo-switch-sports-resort-switch-2/"
            return httpx2.Response(200, text=(ОБРАЗЦЫ / "eshop_product.html").read_text("utf-8"))
        return httpx2.Response(200, json=образец("eshop_upcoming.json")["hits"][0])

    игра = _прогнать(отвечать, lambda к: es.игра(к, США, "7100125145"))
    assert игра is not None and игра.номер == 70010000125145
    assert игра.описание.startswith("Gently swing your racket")

    def без_описания(запрос):
        if запрос.url.host == "www.nintendo.com":
            return httpx2.Response(200, text="<html></html>")
        return httpx2.Response(200, json=образец("eshop_upcoming.json")["hits"][0])

    игра = _прогнать(без_описания, lambda к: es.игра(к, США, "7100125145"))
    assert игра.описание.startswith("Get moving")


def test_повтор_после_429(monkeypatch):
    monkeypatch.setattr(ss, "ПАУЗЫ", (0.0, 0.0, 0.0))
    коды = [429, 429, 200]
    вызовы = []

    def отвечать(запрос):
        вызовы.append(запрос)
        код = коды[len(вызовы) - 1]
        return httpx2.Response(код, json=образец("eshop_upcoming.json") if код == 200 else {})

    игры = _прогнать(отвечать, lambda к: es.витрина(к, США, будущие=True, страница=0))
    assert len(игры) == 2 and len(вызовы) == 3

    вызовы.clear()
    коды = [500, 500, 500, 500]
    with pytest.raises(ss.ОшибкаМагазина):
        _прогнать(отвечать, lambda к: es.витрина(к, США, будущие=True, страница=0))
    assert len(вызовы) == 4


def test_пара_в_steam():
    запросы = []
    ответ = образец("steam_storesearch_hollow_knight_en.json")

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=ответ)

    async def прогон(игра):
        async with httpx2.AsyncClient(base_url=ss.МАГАЗИН, transport=httpx2.MockTransport(отвечать)) as к:
            return await es.найти_в_steam(к, игра, страна_steam="KZ")

    игра = ss.Игра(70010000117998, "Hollow Knight – Nintendo Switch 2 Edition")
    assert asyncio.run(прогон(игра)) == 367520
    доводы = dict(запросы[0].url.params)
    assert доводы["term"] == "Hollow Knight" and доводы["l"] == "english"
    assert доводы["cc"] == "kz"

    # Нечёткого совпадения нет: ложное слияние двух игр хуже двух плашек.
    ответ = {"total": 1, "items": [{"id": 1030300, "name": "Hollow Knight: Silksong"}]}
    assert asyncio.run(прогон(игра)) is None
    # Имя без латиницы: пустой ключ ни с чем не сравнивается, и в магазин не ходим.
    запросы.clear()
    assert asyncio.run(прогон(ss.Игра(70010000000001, "ゼルダの伝説"))) is None
    assert запросы == []


def test_пара_в_eshop():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("eshop_find_hollow_knight.json"))

    найдена = _прогнать(отвечать, lambda к: es.найти_в_eshop(к, США, ss.Игра(367520, "Hollow Knight")))
    # Совпадений два — обычное и Switch 2 Edition; берётся первое в выдаче.
    assert найдена is not None and найдена.номер == 70010000003208
    assert es.сравнимое(найдена.имя) == "hollow knight"
    assert json.loads(запросы[0].content)["query"] == "Hollow Knight"

    найдена = _прогнать(отвечать, lambda к: es.найти_в_eshop(к, США, ss.Игра(1, "Hollow Knight 3")))
    assert найдена is None


def test_цены_ошибка_api_ec_404():
    # Замер: неизвестная `api.ec` страна — HTTP 404 с полем `error`.
    def отвечать(запрос):
        return httpx2.Response(404, json={"error": {"code": "10998", "message": "0998"}})

    with pytest.raises(ss.ОшибкаМагазина, match="10998"):
        _прогнать(отвечать, lambda к: es.цены(к, США, [70010000000185]))


def test_цены_ошибка_api_ec_400():
    def отвечать(запрос):
        return httpx2.Response(400, json={"error": {"code": 4004, "message": "Bad request: Over ids limit number"}})

    with pytest.raises(ss.ОшибкаМагазина, match="Over ids limit number"):
        _прогнать(отвечать, lambda к: es.цены(к, США, [70010000000185]))


def test_запись_с_неразборной_датой():
    игра = es.разобрать_запись({"nsuid": "70010000000001", "title": "x", "releaseDate": "TBD"}, США)
    assert игра is not None and игра.дата is None and игра.дата_текст == "TBD"


def test_скидка_не_число():
    игра = es.разобрать_запись({"nsuid": "70010000000001", "title": "x",
                                "price": {"finalPrice": 9.99, "regPrice": 19.99, "percentOff": "50%"}}, США)
    assert игра is not None and игра.скидка == 0


def test_цены_без_номера_пропущены():
    обычная = {"currency": "USD", "raw_value": "9.99"}
    данные = {"prices": [
        {"sales_status": "onsale", "regular_price": обычная},
        {"title_id": "не номер", "sales_status": "onsale", "regular_price": обычная},
        {"title_id": 7, "sales_status": "onsale", "regular_price": обычная},
    ]}
    assert list(es.разобрать_цены(данные)) == [7]
