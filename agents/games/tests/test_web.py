"""Веб без сети и ключей: календарь, состояние, оценки, вход, ключи."""

import urllib.parse
from datetime import date, datetime, timezone

import pytest
from starlette.testclient import TestClient

import games_db
import secrets_store
import steam_account
import steam_store as ss
import taste
import web


@pytest.fixture
def клиент(monkeypatch):
    async def ничего():
        return None

    async def ollama():
        return {"служба": True, "память": 18, "выбрана": "qwen3.5:9b-q4_K_M", "выбрана_есть": True,
                "векторы": True, "вариант": True, "сборки": []}

    async def пересмотр(игра, причина, род):
        return {"вкус_записан": True, "пересмотр": "поправил"}

    monkeypatch.setattr(web, "_поднять_при_старте", ничего)
    monkeypatch.setattr(web, "_ollama", ollama)
    monkeypatch.setattr(web, "_пересмотр", пересмотр)
    async def в_индекс(*а):
        return None

    monkeypatch.setattr(web, "_в_индекс", в_индекс)
    monkeypatch.setattr(web, "_синхронизировать_сейчас", ничего)
    # Задания из кнопок — свои у каждой проверки: задача прежней проверки живёт в закрытом цикле.
    monkeypatch.setattr(web.ПРИЛОЖЕНИЕ, "задания", {})
    with TestClient(web.app, base_url="http://127.0.0.1:8791") as к:
        yield к


def игра(номер, *, доля=0.9, вышла=True, дата=None):
    return ss.Игра(номер=номер, имя=f"игра {номер}", вышла=вышла, дата=дата,
                   отзывы=ss.Отзывы("", int(доля * 1000), 1000))


def test_ics_событие_на_дату_и_без_даты_нет():
    записи = [
        {"номер": 1, "имя": "Игра, с запятой", "дата": "2027-03-04", "причина": "по вкусу",
         "ссылка": "https://store.steampowered.com/app/1/"},
        {"номер": 2, "имя": "Без даты", "дата": None, "причина": "", "ссылка": "x"},
    ]
    текст = web.календарь_ics(записи, datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert текст.count("BEGIN:VEVENT") == 1
    assert "DTSTART;VALUE=DATE:20270304" in текст and "SUMMARY:Выход: Игра\\, с запятой" in текст


def test_ics_событие_по_магазину():
    адрес_eshop = "https://www.nintendo.com/us/store/products/igra-switch/"
    записи = [
        {"номер": 70010000130792, "магазин": "eshop", "магазины": ["eshop"], "имя": "Игра",
         "дата": "2027-03-04", "причина": "", "ссылка": адрес_eshop},
        {"номер": 5, "магазин": "steam", "магазины": ["steam"], "имя": "Стим",
         "дата": "2027-03-05", "причина": "", "ссылка": "https://store.steampowered.com/app/5/"},
        {"номер": 70010000000001, "магазин": "eshop", "магазины": ["steam", "eshop"],
         "имя": "Парная", "дата": "2027-03-06", "причина": "", "ссылка": "https://example/p"},
    ]
    текст = web.календарь_ics(записи, datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert "UID:eshop-70010000130792@games-agent" in текст
    assert f"URL:{адрес_eshop}" in текст and "SUMMARY:Выход: Игра (Switch)" in текст
    assert "UID:steam-5@games-agent" in текст
    assert "SUMMARY:Выход: Стим\r\n" in текст and "Стим (Switch)" not in текст
    assert "UID:eshop-70010000000001@games-agent" in текст
    assert "SUMMARY:Выход: Парная\r\n" in текст and "Парная (Switch)" not in текст


def test_порядок_по_оценке_и_плюс(клиент):
    с = games_db.открыть()
    for н, доля in ((5, 0.6), (6, 0.97)):
        games_db.запомнить_игру(с, игра(н, доля=доля))
        games_db.предложить(с, н, "")
    д = клиент.get("/api/state").json()
    assert [и["номер"] for и in д["предложения"]] == [6, 5] and д["предложения"][0]["оценка_steamdb"] > 90
    assert клиент.post("/api/rate", json={"номер": 6, "плюс": True}).status_code == 200
    assert [и["номер"] for и in клиент.get("/api/state").json()["желаемое"]] == [6]
    assert клиент.post("/api/rate", json={"номер": 999, "плюс": True}).status_code == 404


def test_минус_на_игре_календаря_убирает_отовсюду(клиент):
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(8, вышла=False, дата=date(2027, 3, 4)))
    games_db.в_календарь(с, 8, "")
    assert "steam-8@" in клиент.get("/calendar.ics").text
    ответ = клиент.post("/api/rate", json={"номер": 8, "плюс": False, "причина": "скучно"})
    assert ответ.status_code == 200 and ответ.json()["вкус_записан"]
    assert клиент.get("/api/state").json()["календарь"] == []
    assert "steam-8@" not in клиент.get("/calendar.ics").text
    assert 8 in taste.запреты(web.каталог())


def test_уже_играл_не_понравилась_запрет(клиент):
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(9))
    games_db.предложить(с, 9, "")
    assert клиент.post("/api/played", json={"номер": 9, "понравилась": False}).status_code == 200
    assert клиент.get("/api/state").json()["предложения"] == [] and 9 in taste.запреты(web.каталог())


def test_вход_подделка_и_повтор(клиент, monkeypatch):
    async def проверить(к, доводы, возврат):
        return "76561197960287930" if доводы.get("openid.sig") == "годна" else None

    monkeypatch.setattr(steam_account, "проверить_вход", проверить)

    def начать():
        адрес = клиент.get("/auth/steam/start", follow_redirects=False).headers["location"]
        возврат = urllib.parse.parse_qs(urllib.parse.urlparse(адрес).query)["openid.return_to"][0]
        return urllib.parse.parse_qs(urllib.parse.urlparse(возврат).query)["state"][0]

    # Вход, который этот браузер не начинал, — отказ: чужой подписанный ответ не входит.
    assert клиент.get("/auth/steam", params={"state": "чужая", "openid.response_nonce": "0", "openid.sig": "годна"}, follow_redirects=False).status_code == 403
    assert клиент.get("/auth/steam", params={"state": начать(), "openid.response_nonce": "1", "openid.sig": "нет"}, follow_redirects=False).status_code == 403
    метка = начать()
    assert клиент.get("/auth/steam", params={"state": метка, "openid.response_nonce": "2", "openid.sig": "годна"}, follow_redirects=False).status_code == 302
    assert secrets_store.загрузить().steamid == "76561197960287930"
    # Та же метка второй раз, человек уже вошёл («Назад» в браузере) — на главную, без входа.
    async def чужой(к, доводы, возврат):
        return "76561190000000000"

    monkeypatch.setattr(steam_account, "проверить_вход", чужой)
    ответ = клиент.get("/auth/steam", params={"state": метка, "openid.response_nonce": "3", "openid.sig": "годна"}, follow_redirects=False)
    assert ответ.status_code == 302 and ответ.headers["location"] == "/"
    assert secrets_store.загрузить().steamid == "76561197960287930"
    monkeypatch.setattr(steam_account, "проверить_вход", проверить)
    # Тот же ответ Steam с новой меткой — отказ: перехваченный ответ не входит повторно.
    assert клиент.get("/auth/steam", params={"state": начать(), "openid.response_nonce": "2", "openid.sig": "годна"}, follow_redirects=False).status_code == 400


def test_ключ_не_уходит_на_страницу(клиент, monkeypatch):
    async def годен(ключ):
        return None

    monkeypatch.setattr(web, "_проверить_deepseek", годен)
    ответ = клиент.post("/api/keys", json={"deepseek": "sk-тайна-123"})
    assert ответ.json()["итоги"]["deepseek"] == "принят" and "sk-тайна-123" not in ответ.text
    состояние = клиент.get("/api/state")
    assert состояние.json()["настройка"]["ключи"]["deepseek"] is True and "sk-тайна-123" not in состояние.text
    assert secrets_store.загрузить().deepseek_ключ == "sk-тайна-123"


def test_негодный_ключ_не_ложится_в_файл(клиент, monkeypatch):
    async def негоден(ключ):
        return "ключ DeepSeek не принят"

    monkeypatch.setattr(web, "_проверить_deepseek", негоден)
    assert клиент.post("/api/keys", json={"deepseek": "плохой"}).json()["итоги"]["deepseek"] == "ключ DeepSeek не принят"
    assert secrets_store.загрузить().deepseek_ключ == ""


def test_чужой_сайт_не_пишет(клиент):
    """Разбор рисков: «простой» запрос чужой страницы и подмена DNS."""
    assert клиент.post("/api/keys", content='{"itad":""}', headers={"Content-Type": "text/plain"}).status_code == 403
    assert клиент.post("/api/logout", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert клиент.post("/api/logout", json={}, headers={"Origin": "http://127.0.0.1:8791"}).status_code == 200
    assert клиент.get("/api/state", headers={"Host": "rebind.evil.example:8791"}).status_code == 400


def test_кнопка_задания(клиент, monkeypatch):
    import jobs

    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ")
    с.close()

    async def задание(род, каталог):
        raise jobs.УжеИдёт("задание «day» уже идёт")

    monkeypatch.setattr(jobs, "запустить", задание)
    assert клиент.post("/api/run/day", json={}).status_code == 202
    assert клиент.post("/api/run/xx", json={}).status_code == 400
    import time
    for _ in range(50):
        if any("уже идёт в службе" in з["текст"] for з in клиент.get("/api/state").json()["лента"]):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("строки «уже идёт» в ленте нет")


def test_пути_моделей(клиент, monkeypatch):
    import models

    assert клиент.post("/api/models/pull", json={"имя": "чужая:модель"}).status_code == 400
    assert клиент.post("/api/models/choose", json={"имя": "чужая:модель"}).status_code == 400
    assert клиент.post("/api/models/choose", json={"имя": models.ПО_УМОЛЧАНИЮ}, headers={"Origin": "https://evil.example"}).status_code == 403

    async def не_собрался(к, имя):
        raise RuntimeError("нет места на диске")

    monkeypatch.setattr(models, "вариант_для_разговора", не_собрался)
    ответ = клиент.post("/api/models/choose", json={"имя": models.ПО_УМОЛЧАНИЮ})
    assert ответ.status_code == 502 and "выбор не изменён" in ответ.json()["error"]
    с = games_db.открыть()
    assert games_db.отметка(с, models.ОТМЕТКА) is None

    async def собрался(к, имя):
        return None

    monkeypatch.setattr(models, "вариант_для_разговора", собрался)
    assert клиент.post("/api/models/choose", json={"имя": models.ПО_УМОЛЧАНИЮ}).status_code == 200
    assert games_db.отметка(с, models.ОТМЕТКА) == models.ПО_УМОЛЧАНИЮ


def test_смена_облачного_ключа_поднимает_советника(клиент, monkeypatch):
    поднято = []

    async def поднять(**к):
        поднято.append(1)

    async def годен(ключ):
        return None

    monkeypatch.setattr(web.ПРИЛОЖЕНИЕ, "агент", object())
    monkeypatch.setattr(web.ПРИЛОЖЕНИЕ, "поднять", поднять)
    monkeypatch.setattr(web, "_проверить_deepseek", годен)
    клиент.post("/api/keys", json={"deepseek": "sk-проба"})
    клиент.post("/api/keys", json={"deepseek": ""})
    assert len(поднято) == 2


def test_вкус_ждёт_подтверждения(клиент):
    assert клиент.get("/api/state").json()["вкус_подтверждён"] is False
    assert клиент.post("/api/taste/confirm", json={}).status_code == 200
    assert клиент.get("/api/state").json()["вкус_подтверждён"] is True


def test_выключатель_eshop(клиент):
    с = games_db.открыть()
    assert клиент.get("/api/state").json()["настройка"]["eshop"] is True
    assert клиент.post("/api/stores", json={"eshop": "нет"}).status_code == 400
    ответ = клиент.post("/api/stores", json={"eshop": False})
    assert ответ.status_code == 200 and ответ.json() == {"итог": "eShop выключен"}
    assert games_db.eshop_включён(с) is False
    assert клиент.get("/api/state").json()["настройка"]["eshop"] is False
    assert клиент.post("/api/stores", json={"eshop": True}).json() == {"итог": "eShop включён"}
    assert games_db.eshop_включён(с) is True
    с.close()


def _разрешено():
    import json

    файл = web.каталог() / ".claude" / "settings.json"
    return json.loads(файл.read_text("utf-8"))["permissions"]["allow"]


def test_выключатель_eshop_переписывает_разрешения_советника(клиент):
    # Страны eShop нет — `eshop_find` не разрешён и при включённом eShop.
    assert "mcp__steam__eshop_find" not in _разрешено()
    с = games_db.открыть()
    games_db.задать_страны(с, eshop="US")
    с.close()
    клиент.post("/api/stores", json={"eshop": True})
    assert "mcp__steam__eshop_find" in _разрешено()
    клиент.post("/api/stores", json={"eshop": False})
    assert "mcp__steam__eshop_find" not in _разрешено()
    клиент.post("/api/stores", json={"eshop": True})
    assert "mcp__steam__eshop_find" in _разрешено()


@pytest.fixture(params=["версия 3", "пустой"])
def старый_файл(request):
    from test_advisor import файл_версии_3

    if request.param == "версия 3":
        файл_версии_3(games_db.путь())
    else:
        games_db.путь().write_bytes(b"")


def test_веб_поднимается_на_старом_файле(старый_файл, клиент):
    assert клиент.get("/api/state").status_code == 200
    assert "mcp__steam__eshop_find" not in _разрешено()  # в старом файле страны eShop нет
    с = games_db.открыть(только_чтение=True)
    assert с.execute("SELECT значение FROM meta WHERE ключ = 'версия_схемы'").fetchone()[0] == str(games_db.ВЕРСИЯ_СХЕМЫ)
    с.close()


def test_выключенный_eshop_прячет_игры_и_пары(клиент):
    с = games_db.открыть()
    # Steam с парой eShop, одиночная eShop и одиночная Steam — в каждом из трёх списков.
    games_db.запомнить_игру(с, ss.Игра(номер=367520, имя="Hollow Knight", вышла=False,
                                       дата=date(2027, 3, 4), цена=46500, валюта="RUB"))
    games_db.запомнить_игру(с, ss.Игра(
        номер=70010000000001, имя="Hollow Knight", цена=1999, валюта="USD",
        адрес="https://www.nintendo.com/us/store/products/hollow-knight-switch/"))
    games_db.записать_пару(с, 70010000000001, 367520)
    games_db.запомнить_игру(с, ss.Игра(
        номер=70010000000002, имя="Pikmin 4", вышла=False, дата=date(2027, 3, 5), цена=5999,
        валюта="USD", адрес="https://www.nintendo.com/us/store/products/pikmin-4-switch/"))
    games_db.записать_пару(с, 70010000000002, None)
    games_db.запомнить_игру(с, игра(5, вышла=False, дата=date(2027, 3, 6)))
    for н in (367520, 70010000000002, 5):
        games_db.в_календарь(с, н, "")
    for н in (11, 70010000000011):
        games_db.запомнить_игру(с, ss.Игра(номер=н, имя=f"п {н}"))
        games_db.предложить(с, н, "")
    games_db.предложить(с, 367520, "")
    # Игра eShop, пару которой в Steam нашли позже, — при выключенном не видна.
    games_db.предложить(с, 70010000000001, "")
    for н in (12, 70010000000012):
        games_db.запомнить_игру(с, ss.Игра(номер=н, имя=f"ж {н}"))
        games_db.предложить(с, н, "")
        клиент.post("/api/rate", json={"номер": н, "плюс": True})

    д = клиент.get("/api/state").json()
    assert {з["номер"] for з in д["календарь"]} == {367520, 70010000000002, 5}
    assert "eshop-70010000000002@" in клиент.get("/calendar.ics").text
    assert 70010000000001 in {з["номер"] for з in д["предложения"]}

    клиент.post("/api/stores", json={"eshop": False})
    д = клиент.get("/api/state").json()
    assert {з["номер"] for з in д["предложения"]} == {11, 367520}
    assert {з["номер"] for з in д["желаемое"]} == {12}
    assert {з["номер"] for з in д["календарь"]} == {367520, 5}
    for список in ("предложения", "желаемое", "календарь"):
        for з in д[список]:
            assert з["магазины"] == ["steam"]
            if з["номер"] == 367520:
                assert not any(к == "пара" or к.startswith("пара_") for к in з), з
            else:
                assert з.get("пара") is None
    текст = клиент.get("/calendar.ics").text
    assert "eshop-" not in текст and "steam-367520@" in текст and "steam-5@" in текст

    # Снова включили — записи eShop и поля пары вернулись.
    клиент.post("/api/stores", json={"eshop": True})
    д = клиент.get("/api/state").json()
    assert {з["номер"] for з in д["предложения"]} == {11, 70010000000011, 367520, 70010000000001}
    assert {з["номер"] for з in д["желаемое"]} == {12, 70010000000012}
    assert {з["номер"] for з in д["календарь"]} == {367520, 70010000000002, 5}
    hk = next(з for з in д["календарь"] if з["номер"] == 367520)
    assert any(к == "пара" or к.startswith("пара_") for к in hk) and hk["магазины"] != ["steam"]
    assert "eshop-70010000000002@" in клиент.get("/calendar.ics").text
    с.close()


def test_проверка_ключа_itad_в_стране_steam(monkeypatch):
    """Проверка ключа IsThereAnyDeal — запрос минимума в стране Steam из хранилища; страны нет —
    понятная жалоба без запроса."""
    import asyncio

    import itad

    страны: list[str] = []

    async def минимум(к, ключ, номер, *, страна):
        страны.append(страна)
        return None

    monkeypatch.setattr(itad, "минимум", минимум)
    assert asyncio.run(web._проверить_itad("ключ")) == "страна Steam не выбрана — укажите в настройках агента"
    assert страны == []
    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ")
    с.close()
    assert asyncio.run(web._проверить_itad("ключ")) is None
    assert страны == ["KZ"]


def test_кнопка_задания_без_страны_steam(клиент, monkeypatch):
    """Страны Steam нет — кнопка не молчит: ответ с понятным текстом, задание не запускается."""
    import jobs

    вызвано: list[str] = []

    async def задание(род, каталог):
        вызвано.append(род)
        return "итог"

    monkeypatch.setattr(jobs, "запустить", задание)
    ответ = клиент.post("/api/run/week", json={})
    assert ответ.status_code == 409
    assert ответ.json()["error"] == "страна Steam не выбрана — укажите в настройках агента"
    assert вызвано == []


# ---- регионы ----


def _ждать(условие):
    import time

    for _ in range(100):
        if условие():
            return
        time.sleep(0.02)
    raise AssertionError("не дождался")


def test_регионы_первая_установка_сразу(клиент):
    assert "mcp__steam__eshop_find" not in _разрешено()
    ответ = клиент.post("/api/regions", json={"steam": "KZ", "eshop": "US"})
    assert ответ.status_code == 200 and ответ.json() == {"итог": "Регион задан: Steam KZ, eShop US"}
    с = games_db.открыть()
    assert (games_db.страна_steam(с), games_db.страна_eshop(с)) == ("KZ", "US")
    с.close()
    assert "region" not in web.ПРИЛОЖЕНИЕ.задания
    # Разрешения советника — по стране eShop: `eshop_find` появился.
    assert "mcp__steam__eshop_find" in _разрешено()


@pytest.mark.parametrize("тело", [
    {"steam": "K", "eshop": "US"}, {"steam": "ЖЖ", "eshop": "US"}, {"steam": 5, "eshop": "US"},
    {"steam": "KZZ", "eshop": None}, {"steam": "KZ", "eshop": "RU"}, {"steam": "KZ", "eshop": 5},
    {"steam": "KZ"}, {"eshop": "US"}, {"steam": "K1", "eshop": None},
])
def test_регионы_неверные_страны(клиент, тело):
    ответ = клиент.post("/api/regions", json=тело)
    assert ответ.status_code == 400 and ответ.json()["error"]
    с = games_db.открыть()
    assert games_db.страна_steam(с) is None
    с.close()


def test_регионы_смена_запускает_задание(клиент, monkeypatch):
    import advisor
    import jobs

    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ", eshop="US")
    с.close()
    порядок: list = []

    async def задание(род, каталог, *, новые):
        порядок.append((род, новые))
        return "Регион сменён: обновлено 0"

    monkeypatch.setattr(jobs, "запустить", задание)
    monkeypatch.setattr(advisor, "подготовить_каталог", lambda к: порядок.append("разрешения"))
    ответ = клиент.post("/api/regions", json={"steam": "de", "eshop": None})
    assert ответ.status_code == 202 and ответ.json() == {"итог": "Пересобираю данные под новый регион…"}
    _ждать(lambda: "разрешения" in порядок)
    assert порядок == [("region", {"steam": "DE", "eshop": None}), "разрешения"]


def test_регионы_при_идущем_задании(клиент, monkeypatch):
    import jobs

    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ", eshop="US")
    с.close()
    вызвано: list = []

    async def задание(*а, **п):
        вызвано.append(а)

    monkeypatch.setattr(jobs, "запустить", задание)
    замок = jobs._замок("week")
    try:
        ответ = клиент.post("/api/regions", json={"steam": "DE", "eshop": "US"})
    finally:
        замок.close()
    assert ответ.status_code == 409 and "неделя" in ответ.json()["error"]
    assert вызвано == []


def test_регионы_сбой_задания_разрешения_всё_равно(клиент, monkeypatch):
    """Задание упало (строка сбоя — его дело) — разрешения советника всё равно по хранилищу."""
    import advisor
    import jobs

    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ", eshop="US")
    с.close()
    порядок: list = []

    async def задание(род, каталог, *, новые):
        raise RuntimeError("магазин упал")

    monkeypatch.setattr(jobs, "запустить", задание)
    monkeypatch.setattr(advisor, "подготовить_каталог", lambda к: порядок.append("разрешения"))
    assert клиент.post("/api/regions", json={"steam": "DE", "eshop": "US"}).status_code == 202
    _ждать(lambda: порядок == ["разрешения"])


def test_кнопки_заданий_и_регион(клиент, monkeypatch):
    import jobs

    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ")
    с.close()
    assert клиент.post("/api/run/region", json={}).status_code == 400
    замок = jobs._замок("region")
    try:
        ответ = клиент.post("/api/run/day", json={})
        assert ответ.status_code == 409 and "регион" in ответ.json()["error"]
        assert "region" in клиент.get("/api/state").json()["задания_идут"]
    finally:
        замок.close()


def test_состояние_регионов(клиент):
    import eshop

    н = клиент.get("/api/state").json()["настройка"]
    assert н["страна_steam"] is None and н["страна_eshop"] is None and н["страна_профиля"] is None
    assert {"страна": "US", "зона": "америки"} in н["страны_eshop"]
    assert {"страна": "GB", "зона": "европа"} in н["страны_eshop"]
    assert len(н["страны_eshop"]) == len(eshop.СТРАНЫ)
    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ", eshop="GB")
    games_db.поставить_отметку(с, "страна_профиля", "KZ")
    с.close()
    н = клиент.get("/api/state").json()["настройка"]
    assert (н["страна_steam"], н["страна_eshop"], н["страна_профиля"]) == ("KZ", "GB", "KZ")


def test_мастер_без_страны_steam_не_пройден(клиент):
    ответ = клиент.post("/api/setup/done", json={})
    assert ответ.status_code == 400 and "страну Steam" in ответ.json()["error"]
    assert клиент.get("/api/state").json()["мастер_пройден"] is False
    с = games_db.открыть()
    # Отметка прежней установки без страны: мастер всё равно не пройден.
    games_db.поставить_отметку(с, "мастер_пройден", "2026-10-01T00:00:00Z")
    assert клиент.get("/api/state").json()["мастер_пройден"] is False
    games_db.задать_страны(с, steam="KZ")
    с.close()
    assert клиент.get("/api/state").json()["мастер_пройден"] is True
    assert клиент.post("/api/setup/done", json={}).status_code == 200


def test_страна_профиля_один_раз(monkeypatch):
    """После входа с ключом — страна профиля один раз, отметкой; сбой — подсказки нет, строки в
    ленте нет, следующий вход спросит снова."""
    import asyncio

    import jobs

    secrets_store.сохранить(secrets_store.Тайны(steam_ключ="ключ", steamid="7656"))
    ответы: list = [RuntimeError("Steam молчит"), "KZ"]
    спрошено: list = []

    async def страна_профиля(к, ключ, steamid):
        спрошено.append((ключ, steamid))
        ответ = ответы.pop(0)
        if isinstance(ответ, Exception):
            raise ответ
        return ответ

    async def синхронизировать(с, учётка):
        return None

    monkeypatch.setattr(steam_account, "страна_профиля", страна_профиля)
    monkeypatch.setattr(jobs, "_синхронизировать", синхронизировать)
    asyncio.run(web._синхронизировать_сейчас())
    с = games_db.открыть()
    assert games_db.отметка(с, "страна_профиля") is None and games_db.лента(с) == []
    asyncio.run(web._синхронизировать_сейчас())
    asyncio.run(web._синхронизировать_сейчас())
    assert games_db.отметка(с, "страна_профиля") == "KZ"
    assert спрошено == [("ключ", "7656")] * 2
    с.close()


def test_профиль_без_страны_не_спрашивается_снова(monkeypatch):
    import asyncio

    import jobs

    secrets_store.сохранить(secrets_store.Тайны(steam_ключ="ключ", steamid="7656"))
    спрошено: list = []

    async def страна_профиля(к, ключ, steamid):
        спрошено.append(1)
        return None

    async def синхронизировать(с, учётка):
        return None

    monkeypatch.setattr(steam_account, "страна_профиля", страна_профиля)
    monkeypatch.setattr(jobs, "_синхронизировать", синхронизировать)
    asyncio.run(web._синхронизировать_сейчас())
    asyncio.run(web._синхронизировать_сейчас())
    assert спрошено == [1]


class _Идёт:
    """Задание, идущее в вебе: `ПРИЛОЖЕНИЕ.задания` видит его не законченным."""

    def done(self):
        return False


@pytest.mark.parametrize("род", ["week", "region"])
def test_регионы_при_задании_в_вебе(клиент, monkeypatch, род):
    import jobs

    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ", eshop="US")
    с.close()
    вызвано: list = []

    async def задание(*а, **п):
        вызвано.append(а)

    monkeypatch.setattr(jobs, "запустить", задание)
    web.ПРИЛОЖЕНИЕ.задания[род] = _Идёт()
    try:
        ответ = клиент.post("/api/regions", json={"steam": "DE", "eshop": "US"})
        кнопка = клиент.post("/api/run/day", json={})
    finally:
        del web.ПРИЛОЖЕНИЕ.задания[род]
    assert ответ.status_code == 409 and jobs.ИМЕНА[род] in ответ.json()["error"]
    if род == "region":
        assert кнопка.status_code == 409 and "регион" in кнопка.json()["error"]
        assert вызвано == []
    else:
        # При идущей «неделе» кнопка дня законно идёт; «регион» не запущен.
        assert кнопка.status_code == 202
        assert all(а[:1] != ("region",) for а in вызвано)


@pytest.mark.parametrize("тело, итог", [
    ({"steam": "KZ", "eshop": "US"}, "Регион прежний: Steam KZ, eShop US"),
    ({"steam": "KZ", "eshop": None}, "Регион прежний: Steam KZ, eShop выключен"),
])
def test_регионы_без_перемен_сразу(клиент, тело, итог):
    """Без смены веб не обещает пересборку: ответ 200 с итогом, фонового задания нет."""
    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ", eshop="US")
    ответ = клиент.post("/api/regions", json=тело)
    assert ответ.status_code == 200 and ответ.json() == {"итог": итог}
    assert "region" not in web.ПРИЛОЖЕНИЕ.задания
    assert games_db.eshop_включён(с) is (тело["eshop"] is not None)
    с.close()


def test_страна_профиля_после_синхронизации(monkeypatch):
    import asyncio

    import jobs

    secrets_store.сохранить(secrets_store.Тайны(steam_ключ="ключ", steamid="7656"))
    порядок: list[str] = []

    async def страна_профиля(к, ключ, steamid):
        порядок.append("профиль")
        return "KZ"

    async def синхронизировать(с, учётка):
        порядок.append("библиотека")

    monkeypatch.setattr(steam_account, "страна_профиля", страна_профиля)
    monkeypatch.setattr(jobs, "_синхронизировать", синхронизировать)
    asyncio.run(web._синхронизировать_сейчас())
    assert порядок == ["библиотека", "профиль"]


def test_выход_снимает_страну_профиля(клиент):
    с = games_db.открыть()
    games_db.поставить_отметку(с, "страна_профиля", "KZ")
    assert клиент.post("/api/logout", json={}).status_code == 200
    assert games_db.отметка(с, "страна_профиля") is None
    assert клиент.get("/api/state").json()["настройка"]["страна_профиля"] is None
    с.close()
