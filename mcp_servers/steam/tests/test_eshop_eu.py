"""Nintendo eShop, зона «Европа»: разбор образцов поиска `searching.nintendo-europe.com` и страницы
игры, снятых 2026-10-19 настоящими запросами, и ход запросов на `httpx2.MockTransport`.

Образец будущих снят с `rows=200` (все 105 будущих игр одним ответом), остальное — как шлёт модуль."""

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx2
import pytest

import eshop_eu as eu
import eshop_store as es
import steam_store as ss

ОБРАЗЦЫ = Path(__file__).parent.parent / "образцы"
БРИТАНИЯ = es.регион("GB")
ГЕРМАНИЯ = es.регион("DE")
# День съёмки образцов: игры от 22.10.2026 ещё не вышли.
СЕЙЧАС = datetime(2026, 10, 19, 12, tzinfo=UTC)


def образец(имя: str):
    return json.loads((ОБРАЗЦЫ / имя).read_text("utf-8"))


def записи(имя: str) -> list[dict]:
    return образец(имя)["response"]["docs"]


def по_объекту(игры, объект: str):
    return next(и for и in игры if и.объект == объект)


@pytest.fixture(autouse=True)
def день_образцов(monkeypatch):
    monkeypatch.setattr(eu, "_сейчас", lambda: СЕЙЧАС)


def test_номер_из_списка():
    # 7001… — сама игра, 7007… — набор, 7005… — пакет обновления: первым бывает и набор.
    assert eu.номер(["70070000035974", "70010000117101"]) == 70010000117101
    assert eu.номер(["70010000118322", "70050000069198"]) == 70010000118322
    # Два номера 7001…: первый — платная игра, второй — бесплатный пропуск друга (замер цен).
    # У SPLIT FICTION номер пропуска численно меньше, но стоит вторым: решает порядок, не число.
    assert eu.номер(["70010000097435", "70010000095423"]) == 70010000097435
    assert eu.номер(["70010000095423", "70010000097435"]) == 70010000095423
    # Только набор: продаётся он, его номер и берём.
    assert eu.номер(["70070000036431"]) == 70070000036431
    assert eu.номер([]) is None
    assert eu.номер(None) is None
    assert eu.номер(["нет", 7]) is None


def test_будущие():
    данные = образец("eshop_eu_upcoming.json")
    игры = eu.разобрать_выдачу(данные, СЕЙЧАС)
    # Записи без nsuid (Professor Layton, Quantum League…) не купить: выброшены.
    с_номером = [з for з in данные["response"]["docs"] if з.get("nsuid_txt")]
    assert len(игры) == len(с_номером) < len(данные["response"]["docs"])
    assert "2339757" not in {и.объект for и in игры}

    ff = игры[0]
    assert ff.объект == "3112375" and ff.имя == "FINAL FANTASY RESONANCE"
    assert ff.номер == 70010000117101
    assert ff.платформы == ("Nintendo Switch 2",)
    assert ff.дата == date(2026, 10, 22) and ff.дата_текст == "2026-10-22"
    assert ff.вышла is False
    assert ff.жанры == ("Adventure", "RPG")
    assert ff.описание == "Hope resonates within the crystals"
    assert ff.адрес == "https://www.nintendo.com/en-gb/Games/Nintendo-Switch-2-games/FINAL-FANTASY-RESONANCE-3112375.html"
    assert ff.обложка == данные["response"]["docs"][0]["wishlist_email_banner460w_image_url_s"]
    # Цена — из `api.ec` (`eshop_store.цены`), не из поиска.
    assert ff.цена is None and ff.цена_без_скидки is None and ff.валюта == "" and not ff.бесплатна
    assert по_объекту(игры, "3112390").платформы == ("Nintendo Switch",)

    # Год без дня: `date_from` — заглушка 31 декабря.
    turok = по_объекту(игры, "3017602")
    assert turok.дата is None and turok.дата_текст == "2027" and turok.вышла is False
    # 2050-12-31 — дата не объявлена.
    вор = по_объекту(игры, "1948885")
    assert вор.дата is None and вор.дата_текст == "дата не объявлена" and вор.вышла is False


def test_вышедшие_по_популярности():
    игры = eu.разобрать_выдачу(образец("eshop_eu_popular.json"), СЕЙЧАС)
    assert len(игры) == 50
    assert игры[0].имя.startswith("Fire Emblem")
    mk8 = по_объекту(игры, "1173281")
    assert mk8.номер == 70010000000126 and mk8.вышла
    assert mk8.дата == date(2017, 4, 28) and mk8.платформы == ("Nintendo Switch",)
    # Набор номером 7007… впереди игры 7001….
    assert по_объекту(игры, "3147060").номер == 70010000110124
    assert по_объекту(игры, "3111530").номер == 70070000036431
    # У EA FC 27 жанров в поиске нет.
    assert по_объекту(игры, "3147152").жанры == ()
    # Перевод строки в кратком описании схлопнут.
    assert по_объекту(игры, "3147152").описание == "The World's Game"
    assert all(и.обложка.startswith("https://") for и in игры)


def test_поиск_hollow_knight():
    игры = eu.разобрать_выдачу(образец("eshop_eu_find_hollow_knight.json"), СЕЙЧАС)
    hk = игры[0]
    assert hk.имя == "Hollow Knight" and hk.номер == 70010000003207 and hk.объект == "1125772"
    издание = по_объекту(игры, "3016593")
    assert издание.номер == 70010000118322 and издание.платформы == ("Nintendo Switch 2",)


def test_обложка_по_порядку():
    база = {"type": "GAME", "title": "Игра", "nsuid_txt": ["70010000000001"], "playable_on_txt": ["HAC"]}
    поля = ["wishlist_email_banner460w_image_url_s", "image_url_h2x1_s", "image_url", "image_url_sq_s"]
    for i, поле in enumerate(поля):
        запись = {**база, **{п: f"https://x/{п}" for п in поля[i:]}}
        assert eu.разобрать_запись(запись, СЕЙЧАС).обложка == f"https://x/{поле}"
    assert eu.разобрать_запись(база, СЕЙЧАС).обложка == ""


def test_не_игры_отсеяны():
    база = {"type": "GAME", "title": "Игра", "nsuid_txt": ["70010000000001"], "playable_on_txt": ["HAC"]}
    assert eu.разобрать_запись(база, СЕЙЧАС) is not None
    assert eu.разобрать_запись({**база, "type": "DLC"}, СЕЙЧАС) is None
    # Игра 3DS из поиска по `fs_id`: не Switch.
    assert eu.разобрать_запись({**база, "playable_on_txt": ["CTR"]}, СЕЙЧАС) is None
    assert eu.разобрать_запись({**база, "nsuid_txt": []}, СЕЙЧАС) is None
    assert eu.разобрать_запись("запись", СЕЙЧАС) is None
    # Без даты — вышла, дня нет.
    без_даты = eu.разобрать_запись(база, СЕЙЧАС)
    assert без_даты.вышла and без_даты.дата is None and без_даты.дата_текст == ""
    # Описание из `product_catalog_description_s`, если `excerpt` нет.
    assert eu.разобрать_запись({**база, "product_catalog_description_s": "Кратко"}, СЕЙЧАС).описание == "Кратко"


def test_дата_по_языку():
    база = {"type": "GAME", "title": "Игра", "nsuid_txt": ["70010000000001"], "playable_on_txt": ["HAC"]}
    for текст in ("22.10.2026", "22-10-2026", "22/10/2026"):
        игра = eu.разобрать_запись({**база, "date_from": "2026-10-22T00:00:00Z", "pretty_date_s": текст}, СЕЙЧАС)
        assert игра.дата == date(2026, 10, 22) and игра.дата_текст == "2026-10-22"
    # «n. n. b.» — немецкое «не объявлена»: решает заглушка 2050-12-31, не текст.
    игра = eu.разобрать_запись({**база, "date_from": "2050-12-31T00:00:00Z", "pretty_date_s": "n. n. b."}, СЕЙЧАС)
    assert игра.дата is None and игра.дата_текст == "дата не объявлена"
    # Неразборная дата — как пришла.
    игра = eu.разобрать_запись({**база, "date_from": "мусор", "pretty_date_s": "12/13/2026"}, СЕЙЧАС)
    assert игра.дата is None and игра.дата_текст == "12/13/2026"


def test_выдача_без_response_ошибка():
    with pytest.raises(ss.ОшибкаМагазина):
        eu.разобрать_выдачу({"error": {"msg": "undefined field"}}, СЕЙЧАС)
    with pytest.raises(ss.ОшибкаМагазина):
        eu.разобрать_выдачу({"response": {}}, СЕЙЧАС)


def test_полное_описание_страницы():
    текст = eu.разобрать_описание((ОБРАЗЦЫ / "eshop_eu_page_hollow_knight.html").read_text("utf-8"))
    assert текст.startswith("Hollow Knight on Nintendo Switch includes all content packs")
    assert текст.endswith("traditional 2D animation")
    краткое = записи("eshop_eu_object_1125772.json")[0]["excerpt"]
    assert len(текст) > 3000 > len(краткое)
    assert "<" not in текст and "&" not in текст
    # Оговорки магазина о покупке и предзаказе и соседняя вкладка — не описание.
    assert "pre-order" not in текст and "releaseDate" not in текст and "Team Cherry" not in текст
    assert "you’re" in текст


def test_описание_устойчиво():
    assert eu.разобрать_описание("<html><body>нет вкладки</body></html>") == ""
    assert eu.разобрать_описание("") == ""
    # Скрипт со словом disclaimer внутри обзора не обрезает текст и сам в него не идёт.
    страница = ('<section id="_bersicht" data-section-type="overview"><p>Erster &amp; Teil</p>'
                '<script>var a = "<div class=\\"game-disclaimer-box\\">";</script><p>Zweiter</p>'
                '<div class="gamedisclaimer-box"><p>Kauf</p></div></section><section><p>Galerie</p></section>')
    assert eu.разобрать_описание(страница) == "Erster & Teil Zweiter"


def test_описание_без_видео():
    # Разметка встроенного видео со страниц Mario Kart 8 Deluxe и EA SPORTS FC 27 (2026-10-19).
    страница = (
        '<section id="Overview" data-section-type="overview"><div class="row">'
        '<noscript><div itemprop="video"><h2>Video: <span itemprop="name">EA SPORTS FC&trade; 27</span></h2>'
        '<span itemprop="description">The World\'s Game</span></div></noscript>'
        '<div id="yt-embedded-1" class="yt-embedded col-xs-12"><iframe type="text/html"\n\t\t\tsrc="https://www.youtube-nocookie.com/embed/x"></iframe>'
        '<div id="youtube-1" class="col-xs-12 text-right youtube-sub">'
        '<div class="youtube-sub-text">Subscribe to <span class="youtube-chan-name"></span> on</div>'
        '<div class="youtube-sub-buttonarea"><div class="youtube-sub-button">YouTube</div></div></div></div>'
        '<p>Play Mario Kart wherever you go!</p>'
        '<div class="youtube-sub-text">Abonniere <span></span> auf</div>'
        '<div class="youtube-sub-buttonarea"><div class="youtube-sub-button">YouTube</div></div>'
        '<h2>Video: Trailer</h2><p>Watch it on YouTube today</p><p>Race with friends.</p>'
        '</div></section>'
    )
    assert eu.разобрать_описание(страница) == "Play Mario Kart wherever you go! Race with friends."


class _Учёт(dict):
    """Запись, которая помнит, какие поля спросил разбор."""

    def __init__(self, данные, спрошено):
        super().__init__(данные)
        self.спрошено = спрошено

    def get(self, ключ, *доводы):
        self.спрошено.add(ключ)
        return super().get(ключ, *доводы)

    def __getitem__(self, ключ):
        self.спрошено.add(ключ)
        return super().__getitem__(ключ)


def test_поля_ответа_покрывают_разбор():
    спрошено: set[str] = set()
    for имя in ("eshop_eu_upcoming.json", "eshop_eu_popular.json", "eshop_eu_find_hollow_knight.json"):
        for з in записи(имя):
            eu.разобрать_запись(_Учёт(з, спрошено), СЕЙЧАС)
            # Без картинок и краткого описания разбор спрашивает все запасные поля.
            голая = {к: з[к] for к in ("type", "title", "nsuid_txt", "playable_on_txt") if к in з}
            eu.разобрать_запись(_Учёт(голая, спрошено), СЕЙЧАС)
    поля = set(eu._ПОЛЯ.split(","))
    assert спрошено <= поля, спрошено - поля


def _прогнать(отвечать, работа):
    async def прогон():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(отвечать)) as к:
            return await работа(к)

    return asyncio.run(прогон())


def _доводы(запрос) -> dict[str, list[str]]:
    доводы: dict[str, list[str]] = {}
    for ключ, значение in запрос.url.params.multi_items():
        доводы.setdefault(ключ, []).append(значение)
    return доводы


ИГРЫ = ["type:GAME", "playable_on_txt:(HAC OR BEE)"]


def test_витрина_доводы():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("eshop_eu_upcoming.json"))

    async def работа(к):
        игры = await eu.витрина(к, ГЕРМАНИЯ, будущие=True, страница=2)
        await eu.витрина(к, БРИТАНИЯ, будущие=False, страница=0)
        return игры

    игры = _прогнать(отвечать, работа)
    assert игры[0].номер == 70010000117101
    будущие, вышедшие = запросы
    assert будущие.method == "GET"
    assert f"{будущие.url.scheme}://{будущие.url.host}{будущие.url.path}" == (
        "https://searching.nintendo-europe.com/de/select")
    д = _доводы(будущие)
    assert sorted(д["fq"]) == sorted([*ИГРЫ, "date_from:[NOW TO *]"])
    assert д["sort"] == ["date_from asc"] and д["rows"] == ["50"] and д["start"] == ["100"]
    assert д["q"] == ["*"] and д["wt"] == ["json"]
    assert д["fl"] == [eu._ПОЛЯ]
    assert вышедшие.url.path == "/en/select"
    д = _доводы(вышедшие)
    assert sorted(д["fq"]) == sorted([*ИГРЫ, "downloads_rank_i:[1 TO *]"])
    assert д["sort"] == ["downloads_rank_i asc"] and д["start"] == ["0"]


def test_найти_по_названию():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("eshop_eu_find_hollow_knight.json"))

    игры = _прогнать(отвечать, lambda к: eu.найти_по_названию(к, БРИТАНИЯ, "Hollow Knight"))
    assert len(игры) == 10 and игры[0].номер == 70010000003207
    д = _доводы(запросы[0])
    assert д["q"] == ["Hollow Knight"] and д["rows"] == ["10"] and sorted(д["fq"]) == sorted(ИГРЫ)
    # Знаки языка запросов Solr экранированы: двоеточие не станет полем.
    _прогнать(отвечать, lambda к: eu.найти_по_названию(к, БРИТАНИЯ, 'Zelda: Tears (2) + "A-B"'))
    assert _доводы(запросы[1])["q"] == [r'Zelda\: Tears \(2\) \+ \"A\-B\"']
    # Пустое название — без запроса.
    assert _прогнать(отвечать, lambda к: eu.найти_по_названию(к, БРИТАНИЯ, "  ")) == []
    assert len(запросы) == 2


def test_запись_по_объекту():
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        имя = "eshop_eu_object_1125772.json" if "fs_id:1125772" in _доводы(запрос)["fq"] else "eshop_eu_object_missing.json"
        return httpx2.Response(200, json=образец(имя))

    игра = _прогнать(отвечать, lambda к: eu.запись(к, ГЕРМАНИЯ, "1125772"))
    assert игра.номер == 70010000003207 and игра.объект == "1125772"
    assert запросы[0].url.path == "/de/select"
    assert sorted(_доводы(запросы[0])["fq"]) == ["fs_id:1125772", "type:GAME"]
    assert _прогнать(отвечать, lambda к: eu.запись(к, ГЕРМАНИЯ, "999999999")) is None
    # Не число — в фильтр Solr не идёт.
    assert _прогнать(отвечать, lambda к: eu.запись(к, ГЕРМАНИЯ, "1 OR fs_id:*")) is None
    assert len(запросы) == 2


def test_игра_с_полным_описанием():
    страница = (ОБРАЗЦЫ / "eshop_eu_page_hollow_knight.html").read_text("utf-8")
    адреса = []

    def отвечать(запрос):
        if запрос.url.host == "www.nintendo.com":
            адреса.append(str(запрос.url))
            return httpx2.Response(200, text=страница)
        return httpx2.Response(200, json=образец("eshop_eu_object_1125772.json"))

    игра = _прогнать(отвечать, lambda к: eu.игра(к, БРИТАНИЯ, "1125772"))
    assert игра.описание.startswith("Hollow Knight on Nintendo Switch includes")
    assert адреса == ["https://www.nintendo.com/en-gb/Games/Nintendo-Switch-download-software/Hollow-Knight-1125772.html"]

    def без_страницы(запрос):
        if запрос.url.host == "www.nintendo.com":
            return httpx2.Response(404, text="нет")
        return httpx2.Response(200, json=образец("eshop_eu_object_1125772.json"))

    игра = _прогнать(без_страницы, lambda к: eu.игра(к, БРИТАНИЯ, "1125772"))
    assert игра.описание == записи("eshop_eu_object_1125772.json")[0]["excerpt"].strip()


def test_пара_в_eshop():
    def отвечать(запрос):
        return httpx2.Response(200, json=образец("eshop_eu_find_hollow_knight.json"))

    найдена = _прогнать(отвечать, lambda к: eu.найти_в_eshop(к, БРИТАНИЯ, ss.Игра(367520, "Hollow Knight™")))
    assert найдена.номер == 70010000003207
    assert _прогнать(отвечать, lambda к: eu.найти_в_eshop(к, БРИТАНИЯ, ss.Игра(1, "Hollow"))).номер == 70010000001763
    assert _прогнать(отвечать, lambda к: eu.найти_в_eshop(к, БРИТАНИЯ, ss.Игра(2, "Silent Hill"))) is None
    assert _прогнать(отвечать, lambda к: eu.найти_в_eshop(к, БРИТАНИЯ, ss.Игра(3, "ゼルダ"))) is None


def test_ошибка_поиска(monkeypatch):
    monkeypatch.setattr(ss, "ПАУЗЫ", (0.0,))

    def отвечать(запрос):
        return httpx2.Response(400, json={"error": {"msg": "undefined field nope"}})

    with pytest.raises(ss.ОшибкаМагазина, match="undefined field"):
        _прогнать(отвечать, lambda к: eu.витрина(к, БРИТАНИЯ, будущие=True, страница=0))
