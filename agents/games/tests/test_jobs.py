"""Ход заданий на подменённых магазине, судье и учётке: сети, ключей и местной модели не нужно."""

import asyncio
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import eshop
import eshop_store as es
import games_db
import jobs
import judge
import steam_store as ss
import taste


@pytest.fixture
def среда(tmp_path, monkeypatch):
    monkeypatch.setattr(taste, "критерии", lambda к: ([taste.Критерий(122, "Ролевая игра", 5)], []))
    monkeypatch.setattr(taste, "запреты", lambda к: {2})
    вкус = {"отпечаток": "А"}
    monkeypatch.setattr(taste, "отпечаток", lambda к: вкус["отпечаток"])
    уведомления: list[str] = []

    async def уведомить(текст):
        уведомления.append(текст)

    monkeypatch.setattr(jobs, "уведомить", уведомить)
    # Страны выбраны: задания идут, ветви eShop открыты (выключатель решает сам).
    с = games_db.открыть()
    games_db.задать_страны(с, steam="KZ", eshop="US")
    с.close()
    return tmp_path, уведомления, вкус


def отзывы(доля, всего=1000):
    return ss.Отзывы("", int(доля * всего), всего)


def игра(номер, *, вышла=True, род="game", цена=100000, скидка=0, доля=0.9, дата=None):
    return ss.Игра(номер=номер, имя=f"игра {номер}", вышла=вышла, род=род, цена=цена,
                   цена_без_скидки=100000, скидка=скидка, отзывы=отзывы(доля), дата=дата, валюта="RUB")


def магазин(игры: dict[int, ss.Игра], страницы: list[list[int]] | None = None):
    # Поиск отдаёт номер со сводкой отзывов — сводку берём из самой игры.
    def пары(номера):
        return [(н, игры[н].отзывы if н in игры else ss.НЕТ_ОТЗЫВОВ) for н in номера]

    async def найти(метка, будущие, страница):
        if страницы is not None:
            return пары(страницы[страница]) if страница < len(страницы) else []
        return пары(игры) if страница == 0 else []

    async def взять(номер):
        return игры.get(номер)

    return jobs.Магазин(найти=найти, игра=взять)


def судья(судимые: list[int], подходит=lambda и: True):
    async def с(и):
        судимые.append(и.номер)
        return judge.Вердикт(подходит(и), "по вкусу")
    return с


def test_запрет_и_покупка_не_доходят_до_судьи(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.обновить_библиотеку(с, [SimpleNamespace(номер=3, имя="своя", часы=1.0, часы_2нед=0.0, последний_запуск=None)])
    судимые: list[int] = []
    итог = asyncio.run(jobs.неделя(tmp, магазин({1: игра(1), 2: игра(2), 3: игра(3)}), судья(судимые)))
    assert судимые == [1] and итог.подошло == ["игра 1"]


def test_к_судье_по_убыванию_оценки(среда):
    tmp, _, _ = среда
    судимые: list[int] = []
    игры = {1: игра(1, доля=0.6), 2_0: игра(20, доля=0.95), 30: ss.Игра(номер=30, имя="без отзывов")}
    asyncio.run(jobs.неделя(tmp, магазин({1: игры[1], 20: игры[20], 30: игры[30]}), судья(судимые)))
    assert судимые == [20, 1, 30]


def test_десять_открытых_судья_не_зовётся(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    for н in range(100, 110):
        games_db.запомнить_игру(с, игра(н))
        games_db.предложить(с, н, "")
    судимые: list[int] = []
    итог = asyncio.run(jobs.неделя(tmp, магазин({1: игра(1)}), судья(судимые)))
    assert судимые == [] and "новых не искал" in итог.текст("неделя")


def test_останавливается_на_нужном_числе(среда, monkeypatch):
    tmp, _, _ = среда
    monkeypatch.setattr(jobs, "НУЖНО", 2)
    судимые: list[int] = []
    asyncio.run(jobs.неделя(tmp, магазин({н: игра(н) for н in (1, 3, 4, 5)}), судья(судимые)))
    assert len(судимые) == 2


def test_отказ_помнится_при_том_же_вкусе(среда):
    tmp, _, вкус = среда
    судимые: list[int] = []
    м = магазин({1: игра(1)})
    отказ = судья(судимые, lambda и: False)
    asyncio.run(jobs.неделя(tmp, м, отказ))
    asyncio.run(jobs.неделя(tmp, м, отказ))
    assert судимые == [1]
    вкус["отпечаток"] = "Б"  # вкус поменялся — прежний отказ снова судится
    asyncio.run(jobs.неделя(tmp, м, отказ))
    assert судимые == [1, 1]


def test_первая_страница_с_годными(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(1))
    games_db.предложить(с, 1, "")
    судимые: list[int] = []
    asyncio.run(jobs.неделя(tmp, магазин({5: игра(5)}, страницы=[[1], [5]]), судья(судимые)))
    assert судимые == [5]


def test_сбой_магазина_на_одной_игре(среда):
    tmp, _, _ = среда

    async def взять(номер):
        if номер == 1:
            raise ss.ОшибкаМагазина("HTTP 503")
        return игра(номер)

    м = jobs.Магазин(найти=lambda м, б, стр: asyncio.sleep(0, [(1, ss.НЕТ_ОТЗЫВОВ), (4, ss.НЕТ_ОТЗЫВОВ)] if стр == 0 else []), игра=взять)
    итог = asyncio.run(jobs.неделя(tmp, м, судья([])))
    assert итог.подошло == ["игра 4"] and len(итог.сбои_магазина) == 1


def test_скидка_с_минимумом_один_раз_и_на_список_steam(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    учётка = jobs.Учётка(
        библиотека=lambda: asyncio.sleep(0, []), список_желаемого=lambda: asyncio.sleep(0, [7])
    )

    async def минимум(номер):
        return SimpleNamespace(цена=30000, валюта="RUB", когда="2026-06-01T00:00:00Z")

    м = магазин({7: игра(7, цена=50000, скидка=50)})
    asyncio.run(jobs.день(м, учётка, минимум))
    asyncio.run(jobs.день(м, учётка, минимум))
    скидки = [у for у in уведомления if у.startswith("Скидка")]
    assert len(скидки) == 1 and "минимум был 300 руб. (2026-06-01)" in скидки[0]


def test_день_без_подбора(среда, monkeypatch):
    """День — скидки без суда: подбор игр раз в неделю (решение пользователя 2026-10-19), и
    без «Критериев» день не падает."""
    tmp, уведомления, _ = среда
    monkeypatch.setattr(taste, "критерии", lambda к: ([], []))
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(7))
    games_db.оценить(с, 7, True, "")
    итог = asyncio.run(jobs.день(магазин({1: игра(1), 7: игра(7, цена=50000, скидка=50)})))
    assert any(з["род"] == "скидка" for з in games_db.лента(с))
    assert games_db.открытых(с) == 0 and "новых скидок 1" in итог


def test_выход_сегодня_одно_уведомление(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(8, вышла=False, дата=date.today()))
    games_db.в_календарь(с, 8, "")
    for _ in range(2):
        asyncio.run(jobs.день(магазин({})))
    assert [у for у in уведомления if у.startswith("Сегодня выходит")] == ["Сегодня выходит «игра 8»"]


def test_месяц_обновляет_календарь(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    for н in (8, 9):
        games_db.запомнить_игру(с, игра(н, вышла=False))
        games_db.в_календарь(с, н, "")
    новая_дата = date.today() + timedelta(days=30)
    м = магазин({8: игра(8, вышла=False, дата=новая_дата), 9: игра(9, вышла=True)})
    asyncio.run(jobs.месяц(tmp, м, судья([], lambda и: False)))
    календарь = {з["номер"]: з for з in games_db.календарь(с)}
    assert календарь[8]["дата"] == новая_дата.isoformat() and 9 not in календарь
    assert 9 in [з["номер"] for з in games_db.предложения(с)]


def test_сверка_через_полгода(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    asyncio.run(jobs.неделя(tmp, магазин({}), судья([])))
    assert not [у for у in уведомления if "сверки" in у]
    games_db.поставить_отметку(с, "последняя_сверка", (date.today() - timedelta(days=200)).isoformat())
    asyncio.run(jobs.неделя(tmp, магазин({}), судья([])))
    assert any("сверки" in у for у in уведомления)


def test_без_ключа_steam_одна_строка(среда):
    tmp, _, _ = среда
    for _ in range(2):
        asyncio.run(jobs.день(магазин({})))
    с = games_db.открыть()
    assert sum("Ключа Steam нет" in з["текст"] for з in games_db.лента(с)) == 1


def test_без_критериев_задание_падает(среда, monkeypatch):
    tmp, _, _ = среда
    monkeypatch.setattr(taste, "критерии", lambda к: ([], []))
    with pytest.raises(RuntimeError, match="Критериев"):
        asyncio.run(jobs.неделя(tmp, магазин({}), судья([])))


def test_негодная_игра_не_держит_страницу(среда):
    """Опыт просмотра: страница 0 — [уже предложена, не вышла], страница 1 — [годная]."""
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(1))
    games_db.предложить(с, 1, "")
    игры = {1: игра(1), 7: игра(7, вышла=False, дата=date.today() + timedelta(days=60)), 5: игра(5)}
    судимые: list[int] = []
    for _ in range(2):
        asyncio.run(jobs.неделя(tmp, магазин(игры, страницы=[[1, 7], [5]]), судья(судимые)))
    assert судимые == [5]


def test_не_игра_запоминается(среда):
    tmp, _, _ = среда
    вызовы: list[int] = []

    async def взять(номер):
        вызовы.append(номер)
        return игра(номер, род="demo")

    м = jobs.Магазин(найти=lambda м, б, стр: asyncio.sleep(0, [(3, ss.НЕТ_ОТЗЫВОВ)] if стр == 0 else []), игра=взять)
    asyncio.run(jobs.неделя(tmp, м, судья([])))
    asyncio.run(jobs.неделя(tmp, м, судья([])))
    assert вызовы == [3]


def test_замок_задания(среда):
    замок = jobs._замок("day")
    try:
        with pytest.raises(jobs.УжеИдёт):
            jobs._замок("day")
    finally:
        замок.close()
    jobs._замок("day").close()


def test_неделя_собирает_rag(среда, tmp_path):
    import numpy as np
    import similar

    tmp, _, _ = среда
    индекс = similar.Индекс(lambda тексты: np.ones((len(тексты), 3), np.float32), tmp_path / "s.sqlite")
    с = games_db.открыть()
    games_db.обновить_библиотеку(с, [SimpleNamespace(номер=3, имя="своя", часы=10.0, часы_2нед=0.0, последний_запуск=None)])
    asyncio.run(jobs.неделя(tmp, магазин({}), судья([]), индекс=индекс))
    assert индекс.размер() == 1


def test_последний_срок():
    from datetime import datetime
    assert jobs.последний_срок("day", datetime(2026, 10, 16, 22, 0)) == datetime(2026, 10, 16, 11, 0)
    assert jobs.последний_срок("day", datetime(2026, 10, 16, 9, 0)) == datetime(2026, 10, 15, 11, 0)
    # 2026-10-16 — пятница: до 14:00 срок — прошлая пятница, после — этот день.
    assert jobs.последний_срок("week", datetime(2026, 10, 16, 13, 0)) == datetime(2026, 10, 9, 14, 0)
    assert jobs.последний_срок("week", datetime(2026, 10, 16, 15, 0)) == datetime(2026, 10, 16, 14, 0)
    assert jobs.последний_срок("week", datetime(2026, 10, 19, 9, 0)) == datetime(2026, 10, 16, 14, 0)
    # Месяц — 1-е число 10:00; до срока 1-го числа — прошлый месяц, через год тоже.
    assert jobs.последний_срок("month", datetime(2026, 10, 16, 9, 0)) == datetime(2026, 10, 1, 10, 0)
    assert jobs.последний_срок("month", datetime(2026, 10, 1, 9, 0)) == datetime(2026, 9, 1, 10, 0)
    assert jobs.последний_срок("month", datetime(2027, 1, 1, 9, 0)) == datetime(2026, 12, 1, 10, 0)


def test_догоняющий_запуск(среда):
    from datetime import datetime
    с = games_db.открыть()
    сейчас = datetime(2026, 10, 16, 22, 0)
    assert jobs.нужен_догоняющий(с, "day", сейчас)          # Мак был выключен в 11:00
    games_db.поставить_отметку(с, "day_начато", "2026-10-16T11:00:05")
    assert jobs.нужен_догоняющий(с, "day", сейчас)          # начато и прервано — догнать
    games_db.поставить_отметку(с, "day_сбой", "2026-10-16T11:02:00")
    assert not jobs.нужен_догоняющий(с, "day", сейчас)      # обработанный сбой сам не повторяется
    games_db.поставить_отметку(с, "day_сбой", "")
    games_db.поставить_отметку(с, "day_выполнено", "2026-10-16T11:10:00")
    assert not jobs.нужен_догоняющий(с, "day", сейчас)
    # Вчерашнее выполнение сегодняшний срок не закрывает.
    games_db.поставить_отметку(с, "day_выполнено", "2026-10-15T11:10:00")
    замок = jobs._замок("day")
    try:
        assert not jobs.нужен_догоняющий(с, "day", сейчас)  # идёт сейчас — не трогать
    finally:
        замок.close()
    assert jobs.нужен_догоняющий(с, "day", сейчас)


def test_игра_без_цены_из_списка_steam_запоминается(среда):
    tmp, _, _ = среда
    учётка = jobs.Учётка(библиотека=lambda: asyncio.sleep(0, []), список_желаемого=lambda: asyncio.sleep(0, [9]))
    asyncio.run(jobs.день(магазин({9: игра(9, вышла=False, цена=None)}), учётка))
    с = games_db.открыть()
    assert [з["имя"] for з in games_db.желаемое(с)] == ["игра 9"]


# ---- eShop ----

ЕШОП = 70010000000001  # nsuid: номер eShop


def игра_eshop(номер=ЕШОП, *, вышла=True, дата=None, имя=None, отзывы_=ss.НЕТ_ОТЗЫВОВ, объект=None):
    return ss.Игра(номер=номер, имя=имя or f"игра {номер}", вышла=вышла, дата=дата,
                   отзывы=отзывы_, объект=объект or f"о{номер}", адрес=f"https://www.nintendo.com/{номер}",
                   валюта="USD")


def test_месяц_обновляет_игру_eshop_по_объекту(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра_eshop(вышла=False))
    games_db.в_календарь(с, ЕШОП, "")
    новая_дата = date.today() + timedelta(days=30)
    объекты: list[str] = []
    номера: list[int] = []

    async def игра_по_объекту(объект):
        объекты.append(объект)
        return игра_eshop(вышла=False, дата=новая_дата)

    async def взять(номер):
        номера.append(номер)
        return None

    м = jobs.Магазин(найти=lambda *_: asyncio.sleep(0, []), игра=взять, игра_eshop=игра_по_объекту)
    asyncio.run(jobs.месяц(tmp, м, судья([])))
    assert объекты == [f"о{ЕШОП}"] and ЕШОП not in номера
    assert games_db.календарь(с)[0]["дата"] == новая_дата.isoformat()


class Ешоп:
    """Поддельные поля eShop `Магазин` со счётчиками вызовов."""

    def __init__(self, страницы, пары=None, отзывы_=None):
        self.страницы = страницы  # список страниц витрины: списки игр
        self.пары = пары or {}  # номер eShop → номер Steam
        self.отзывы = отзывы_ or {}
        self.в_steam: list[int] = []
        self.отзывов: list[int] = []

    async def витрина(self, будущие, страница):
        return self.страницы[страница] if страница < len(self.страницы) else []

    async def игра_eshop(self, объект):
        for стр in self.страницы:
            for и in стр:
                if и.объект == объект:
                    return и
        return None

    async def найти_в_steam(self, игра):
        self.в_steam.append(игра.номер)
        return self.пары.get(игра.номер)

    async def отзывы_eshop(self, игра):
        self.отзывов.append(игра.номер)
        return self.отзывы.get(игра.номер, ss.НЕТ_ОТЗЫВОВ)

    def поля(self):
        return dict(витрина=self.витрина, игра_eshop=self.игра_eshop,
                    найти_в_steam=self.найти_в_steam, отзывы_eshop=self.отзывы_eshop)


def страница_eshop(с, е: Ешоп, будущие=False, исключить=frozenset(), сбои=None):
    м = jobs.Магазин(найти=lambda *_: asyncio.sleep(0, []), игра=lambda н: asyncio.sleep(0, None), **е.поля())
    отказы = jobs._Отказы([] if сбои is None else сбои)
    return asyncio.run(jobs._страница_eshop(с, м, будущие, set(исключить), отказы))


def test_eshop_с_парой_в_steam_не_кандидат(среда):
    с = games_db.открыть()
    е = Ешоп([[игра_eshop(ЕШОП), игра_eshop(ЕШОП + 1)]], пары={ЕШОП: 367520})
    assert [н for н, _ in страница_eshop(с, е)] == [ЕШОП + 1]
    assert games_db.пара(с, ЕШОП) == 367520 and games_db.пара(с, 367520) == ЕШОП
    assert games_db.игра(с, ЕШОП)["объект"] == f"о{ЕШОП}"


def test_eshop_сверенная_пара_не_ищется_снова(среда):
    с = games_db.открыть()
    е = Ешоп([[игра_eshop(ЕШОП)]])
    страница_eshop(с, е)
    страница_eshop(с, е)
    assert е.в_steam == [ЕШОП]


def test_eshop_отзывы_только_у_вышедших(среда):
    с = games_db.открыть()
    е = Ешоп([[игра_eshop(ЕШОП), игра_eshop(ЕШОП + 1)]], пары={ЕШОП: 367520},
             отзывы_={ЕШОП + 1: отзывы(0.8, 50)})
    кандидаты = страница_eshop(с, е)
    assert е.отзывов == [ЕШОП + 1] and кандидаты == [(ЕШОП + 1, отзывы(0.8, 50))]
    assert games_db.игра(с, ЕШОП + 1)["отзывы_всего"] == 50
    е2 = Ешоп([[игра_eshop(ЕШОП + 2, вышла=False)]])
    страница_eshop(с, е2, будущие=True)
    assert е2.отзывов == []


def test_eshop_исключённая_не_ищется(среда):
    с = games_db.открыть()
    е = Ешоп([[игра_eshop(ЕШОП), игра_eshop(ЕШОП + 1)]])
    assert [н for н, _ in страница_eshop(с, е, исключить={ЕШОП})] == [ЕШОП + 1]
    assert е.в_steam == [ЕШОП + 1]


def test_eshop_первая_страница_исключена(среда):
    с = games_db.открыть()
    е = Ешоп([[игра_eshop(ЕШОП)], [игра_eshop(ЕШОП + 1)]])
    assert [н for н, _ in страница_eshop(с, е, исключить={ЕШОП})] == [ЕШОП + 1]
    assert страница_eshop(с, Ешоп([[игра_eshop(ЕШОП)]]), исключить={ЕШОП}) == []


def test_eshop_после_сбоя_поиска_пары_несверенные_ждут(среда):
    """Сбой поиска пары — до конца запуска поиск не зовётся: у `_запрос` повторы — минуты на
    отказ. Несверенные записи ждут следующего запуска, сверенные идут как обычно."""
    с = games_db.открыть()
    games_db.записать_пару(с, ЕШОП + 2, None)
    е = Ешоп([[игра_eshop(ЕШОП), игра_eshop(ЕШОП + 1), игра_eshop(ЕШОП + 2)]])
    вызовы: list[int] = []

    async def сбой(игра):
        вызовы.append(игра.номер)
        raise ss.ОшибкаМагазина("HTTP 503")

    е.найти_в_steam = сбой
    сбои: list[str] = []
    assert [н for н, _ in страница_eshop(с, е, сбои=сбои)] == [ЕШОП + 2]
    assert вызовы == [ЕШОП] and not games_db.пара_сверена(с, ЕШОП + 1)
    assert сбои == ["поиск пары не удался: игра 70010000000001: HTTP 503"]


def test_eshop_после_сбоя_metacritic_не_спрашивается(среда):
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра_eshop(ЕШОП + 1, отзывы_=отзывы(0.8, 40)))
    е = Ешоп([[игра_eshop(ЕШОП), игра_eshop(ЕШОП + 1)]])

    async def сбой(игра):
        е.отзывов.append(игра.номер)
        raise ss.ОшибкаМагазина("HTTP 503")

    е.отзывы_eshop = сбой
    сбои: list[str] = []
    кандидаты = dict(страница_eshop(с, е, сбои=сбои))
    assert е.отзывов == [ЕШОП] and кандидаты[ЕШОП + 1].всего == 40  # прежняя оценка осталась
    assert сбои == ["оценка Metacritic не получена: игра 70010000000001: HTTP 503"]


def test_сбой_витрины_eshop_не_роняет_steam(среда):
    tmp, _, _ = среда
    е = Ешоп([])

    async def витрина(будущие, страница):
        raise ss.ОшибкаМагазина("HTTP 503")

    е.витрина = витрина
    м = магазин({1: игра(1)})
    итог = asyncio.run(jobs.неделя(tmp, jobs.Магазин(найти=м.найти, игра=м.игра, **е.поля()), судья([])))
    assert итог.подошло == ["игра 1"] and итог.сбои_магазина == []
    assert итог.сбои_eshop == ["eShop не ответил: HTTP 503"]
    assert "eShop не ответил: HTTP 503" in итог.текст("неделя")


def test_по_очереди():
    о = ss.НЕТ_ОТЗЫВОВ
    assert list(jobs._по_очереди([[(1, о), (2, о), (3, о)], [(2, о), (5, о)], [(9, о)]])) == [1, 2, 9, 5, 3]


def test_месяц_источники_по_очереди(среда, monkeypatch):
    tmp, _, _ = среда
    monkeypatch.setattr(taste, "критерии", lambda к: (
        [taste.Критерий(1, "первая", 5), taste.Критерий(2, "вторая", 3)], []))
    будущие = {н: replace(игра(н, вышла=False), отзывы=ss.НЕТ_ОТЗЫВОВ) for н in (11, 12, 13, 14, 21, 22, 23, 24)}
    по_меткам = {1: [11, 12, 13, 14], 2: [21, 22, 23, 24]}

    async def найти(метка, б, страница):
        return [(н, ss.НЕТ_ОТЗЫВОВ) for н in по_меткам[метка]] if страница == 0 else []

    async def взять(номер):
        return будущие.get(номер)

    е = Ешоп([[игра_eshop(ЕШОП + н, вышла=False) for н in range(4)]])
    судимые: list[int] = []
    asyncio.run(jobs.месяц(tmp, jobs.Магазин(найти=найти, игра=взять, **е.поля()), судья(судимые)))
    assert судимые == [11, 21, ЕШОП, 12, 22, ЕШОП + 1, 13, 23, ЕШОП + 2, 14]


def test_неделя_eshop_с_лучшей_оценкой_первой(среда):
    tmp, _, _ = среда
    е = Ешоп([[игра_eshop(ЕШОП)]], отзывы_={ЕШОП: отзывы(0.99, 500)})
    м = магазин({1: игра(1, доля=0.9), 3: игра(3, доля=0.7)})
    м = jobs.Магазин(найти=м.найти, игра=м.игра, **е.поля())
    судимые: list[int] = []
    итог = asyncio.run(jobs.неделя(tmp, м, судья(судимые)))
    assert судимые == [ЕШОП, 1, 3] and "игра 70010000000001" in итог.подошло
    с = games_db.открыть()
    assert ЕШОП in [з["номер"] for з in games_db.предложения(с)]


def магазин_с_парой(игры, найти_в_eshop):
    м = магазин(игры)
    return jobs.Магазин(найти=м.найти, игра=м.игра, найти_в_eshop=найти_в_eshop)


def test_подошедшая_игра_steam_связана_с_eshop(среда):
    tmp, _, _ = среда
    искали: list[int] = []

    async def найти_в_eshop(и):
        искали.append(и.номер)
        return игра_eshop(имя=и.имя)

    м = магазин_с_парой({1: игра(1)}, найти_в_eshop)
    asyncio.run(jobs.неделя(tmp, м, судья([])))
    с = games_db.открыть()
    assert искали == [1] and games_db.пара(с, 1) == ЕШОП and games_db.пара(с, ЕШОП) == 1
    assert games_db.игра(с, ЕШОП)["объект"] == f"о{ЕШОП}"
    assert [з["номер"] for з in games_db.предложения(с)] == [1]
    assert games_db.предложения(с)[0]["магазины"] == ["steam", "eshop"]


def test_сверенная_пара_steam_не_ищется(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.записать_пару(с, 1, None)
    искали: list[int] = []

    async def найти_в_eshop(и):
        искали.append(и.номер)
        return None

    asyncio.run(jobs.неделя(tmp, магазин_с_парой({1: игра(1)}, найти_в_eshop), судья([])))
    assert искали == [] and [з["номер"] for з in games_db.предложения(с)] == [1]


def test_сбой_поиска_пары_не_отменяет_предложение(среда):
    tmp, _, _ = среда
    вызовы: list[int] = []

    async def найти_в_eshop(и):
        вызовы.append(и.номер)
        raise ss.ОшибкаМагазина("HTTP 503")

    итог = asyncio.run(jobs.неделя(tmp, магазин_с_парой({1: игра(1), 3: игра(3, доля=0.5)}, найти_в_eshop), судья([])))
    с = games_db.открыть()
    assert итог.подошло == ["игра 1", "игра 3"] and not games_db.пара_сверена(с, 1)
    assert вызовы == [1] and итог.сбои_магазина == []
    assert итог.сбои_eshop == ["поиск пары не удался: игра 1: HTTP 503"]
    текст = итог.текст("неделя")
    assert "поиск пары не удался: игра 1: HTTP 503" in текст and "магазин не отдал" not in текст


def test_пара_под_запретом_игру_steam_не_предлагает(среда, monkeypatch):
    tmp, _, _ = среда
    monkeypatch.setattr(taste, "запреты", lambda к: {ЕШОП})

    async def найти_в_eshop(и):
        return игра_eshop(имя=и.имя)

    итог = asyncio.run(jobs.неделя(tmp, магазин_с_парой({1: игра(1)}, найти_в_eshop), судья([])))
    с = games_db.открыть()
    assert итог.подошло == [] and games_db.предложения(с) == [] and итог.сбои_eshop == []
    assert games_db.пара(с, 1) == ЕШОП


def test_пара_предложена_в_этом_запуске_игру_steam_не_предлагает(среда):
    """Игра eShop с лучшей оценкой подошла первой; игра Steam оказалась её парой — вторая
    плашка той же игры не нужна."""
    tmp, _, _ = среда
    е = Ешоп([[игра_eshop(ЕШОП, имя="Общая")]], отзывы_={ЕШОП: отзывы(0.99, 500)})
    м = магазин({1: игра(1)})

    async def найти_в_eshop(и):
        return игра_eshop(имя="Общая")

    м = jobs.Магазин(найти=м.найти, игра=м.игра, найти_в_eshop=найти_в_eshop, **е.поля())
    судимые: list[int] = []
    итог = asyncio.run(jobs.неделя(tmp, м, судья(судимые)))
    с = games_db.открыть()
    assert судимые == [ЕШОП, 1] and [з["номер"] for з in games_db.предложения(с)] == [ЕШОП]
    assert итог.подошло == ["Общая"]


def test_кандидат_eshop_с_появившейся_парой_не_судится(среда):
    """Игра Steam подошла и нашла пару — тот же кандидат eShop ниже в очереди уже не судится."""
    tmp, _, _ = среда
    е = Ешоп([[игра_eshop(ЕШОП, имя="Общая")]])
    м = магазин({1: игра(1)})

    async def найти_в_eshop(и):
        return игра_eshop(имя="Общая")

    м = jobs.Магазин(найти=м.найти, игра=м.игра, найти_в_eshop=найти_в_eshop, **е.поля())
    судимые: list[int] = []
    asyncio.run(jobs.неделя(tmp, м, судья(судимые)))
    с = games_db.открыть()
    assert судимые == [1] and [з["номер"] for з in games_db.предложения(с)] == [1]


def test_дата_словами(monkeypatch):
    monkeypatch.setattr(jobs, "_сегодня", lambda: "2026-10-19")
    assert jobs._дата_словами("2026-11-05") == "5 ноября"
    assert jobs._дата_словами("2026-01-31") == "31 января"
    assert jobs._дата_словами("2025-12-01T10:00:00") == "1 декабря 2025"  # другой год — с годом


def _история(с, номер, цена, когда):
    with с:
        с.execute("INSERT INTO price_history (номер, цена, валюта, отмечено) VALUES (?, ?, 'USD', ?)",
                  (номер, цена, когда))


def test_скидки_eshop_на_желаемое(среда, monkeypatch):
    tmp, уведомления, _ = среда
    monkeypatch.setattr(jobs, "_сегодня", lambda: "2026-10-19")
    с = games_db.открыть()
    # Игра Steam с парой eShop — в своём желаемом; игра eShop без пары — тоже.
    games_db.запомнить_игру(с, игра(7))
    games_db.оценить(с, 7, True, "")
    games_db.запомнить_игру(с, игра_eshop(ЕШОП, имя="Имя в eShop"))
    games_db.записать_пару(с, 7, ЕШОП)
    games_db.запомнить_игру(с, игра_eshop(ЕШОП + 1, имя="Только eShop"))
    games_db.оценить(с, ЕШОП + 1, True, "")
    _история(с, ЕШОП, 2999, "2026-09-01T00:00:00")
    _история(с, ЕШОП + 1, 1499, "2026-09-05T00:00:00")
    запросы: list[list[int]] = []

    async def цены_eshop(номера):
        запросы.append(sorted(номера))
        return {н: es.Цена(1999, 3999, 50, "USD", "2026-11-05") for н in номера}

    м = магазин({7: игра(7)})
    м = jobs.Магазин(найти=м.найти, игра=м.игра, цены_eshop=цены_eshop)
    asyncio.run(jobs.день(м))
    скидки = sorted(у for у in уведомления if у.startswith("Скидка"))
    assert запросы == [[ЕШОП, ЕШОП + 1]]
    assert скидки == [
        "Скидка 50 % на «Только eShop» в eShop: 19.99 USD вместо 39.99 USD, до 5 ноября; "
        "минимум за время наблюдения — 14.99 USD (5 сентября)",
        "Скидка 50 % на «игра 7» в eShop: 19.99 USD вместо 39.99 USD, до 5 ноября; "
        "это минимум за время наблюдения",
    ]
    запись = games_db.игра(с, ЕШОП + 1)
    assert (запись["цена"], запись["цена_без_скидки"], запись["скидка"], запись["скидка_до"]) == (1999, 3999, 50, "2026-11-05")
    asyncio.run(jobs.день(м))  # назавтра та же цена — уведомлений нет
    assert len([у for у in уведомления if у.startswith("Скидка")]) == 2


def test_скидки_eshop_не_приходят_по_игре_со_switch(среда):
    """«Есть на Switch»: скидка eShop ни на собственную запись, ни на пару игры Steam; скидка
    Steam той же игры приходит, как раньше."""
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(7, цена=50000, скидка=50))
    games_db.оценить(с, 7, True, "")
    games_db.запомнить_игру(с, игра_eshop(ЕШОП, имя="Пара семёрки"))
    games_db.записать_пару(с, 7, ЕШОП)
    games_db.запомнить_игру(с, игра_eshop(ЕШОП + 1, имя="Только eShop"))
    games_db.оценить(с, ЕШОП + 1, True, "")
    games_db.запомнить_игру(с, игра_eshop(ЕШОП + 2, имя="Ещё одна"))
    games_db.оценить(с, ЕШОП + 2, True, "")
    games_db.есть_на_switch(с, ЕШОП, True)
    games_db.есть_на_switch(с, ЕШОП + 1, True)
    запросы: list[list[int]] = []

    async def цены_eshop(номера):
        запросы.append(sorted(номера))
        return {н: es.Цена(1999, 3999, 50, "USD", "2026-11-05") for н in номера}

    м = магазин({7: игра(7, цена=50000, скидка=50)})
    м = jobs.Магазин(найти=м.найти, игра=м.игра, цены_eshop=цены_eshop)
    asyncio.run(jobs.день(м))
    assert запросы == [[ЕШОП + 2]]
    скидки = sorted(у for у in уведомления if у.startswith("Скидка"))
    assert len(скидки) == 2
    assert скидки[0].startswith("Скидка 50 % на «Ещё одна» в eShop")
    assert скидки[1].startswith("Скидка 50 % на «игра 7» в Steam")


def test_история_rag_включает_игры_switch(среда):
    """Игры «Есть на Switch» — библиотека: в базе знаний RAG строкой «куплена на Switch»."""
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра_eshop(ЕШОП, имя="Куплена на Switch"))
    games_db.есть_на_switch(с, ЕШОП, True)
    games_db.запомнить_игру(с, игра_eshop(ЕШОП + 1, имя="Не куплена"))
    games_db.оценить(с, ЕШОП + 1, True, "")
    документы = {д.номер: д for д in jobs.документы_истории(с)}
    assert документы[ЕШОП].имя == "Куплена на Switch"
    assert документы[ЕШОП].отметка == "куплена на Switch"
    assert документы[ЕШОП + 1].отметка == "плюс"


def test_список_steam_ищет_пару_eshop_один_раз(среда):
    tmp, _, _ = среда
    учётка = jobs.Учётка(библиотека=lambda: asyncio.sleep(0, []), список_желаемого=lambda: asyncio.sleep(0, [9]))
    искали: list[int] = []

    async def найти_в_eshop(и):
        искали.append(и.номер)
        return None

    for _ in range(2):
        asyncio.run(jobs.день(магазин_с_парой({9: игра(9)}, найти_в_eshop), учётка))
    assert искали == [9]


def test_текст_скидки_steam(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(7))
    games_db.оценить(с, 7, True, "")
    asyncio.run(jobs.день(магазин({7: игра(7, цена=50000, скидка=50)})))
    assert [у for у in уведомления if у.startswith("Скидка")] == ["Скидка 50 % на «игра 7» в Steam: 500 руб. вместо 1 000 руб."]


@pytest.mark.parametrize("страна, страна_steam", [("US", "KZ"), ("DE", "DE")])
def test_запустить_связывает_поля_eshop(среда, monkeypatch, страна, страна_steam):
    """Настоящий запуск без сети: клиенты и функции магазинов подменены, проверяется только
    связка полей `Магазин` с функциями `eshop`, `steam_store`, `itad` и `metacritic` — со
    странами из хранилища."""
    import contextlib

    import itad
    import metacritic
    import secrets_store

    с = games_db.открыть()
    games_db.задать_страны(с, steam=страна_steam, eshop=страна)
    с.close()
    регион = eshop.регион(страна)
    monkeypatch.setattr(secrets_store, "загрузить", lambda: secrets_store.Тайны(itad_ключ="ключ-itad"))

    def клиент(имя):
        @contextlib.asynccontextmanager
        async def к():
            yield имя
        return к

    monkeypatch.setattr(ss, "клиент", клиент("steam"))
    monkeypatch.setattr(eshop, "клиент", клиент("eshop"))
    monkeypatch.setattr(metacritic, "клиент", клиент("metacritic"))
    вызовы: list[tuple] = []

    def запись(имя, ответ):
        async def ф(*доводы, **поля):
            вызовы.append((имя, доводы, поля))
            return ответ
        return ф

    monkeypatch.setattr(eshop, "витрина", запись("витрина", []))
    monkeypatch.setattr(eshop, "игра", запись("игра", None))
    monkeypatch.setattr(eshop, "найти_в_steam", запись("найти_в_steam", None))
    monkeypatch.setattr(eshop, "найти_в_eshop", запись("найти_в_eshop", None))
    monkeypatch.setattr(eshop, "цены", запись("цены", {}))
    monkeypatch.setattr(metacritic, "отзывы", запись("отзывы", ss.НЕТ_ОТЗЫВОВ))
    monkeypatch.setattr(ss, "найти_с_отзывами", запись("найти_с_отзывами", []))
    monkeypatch.setattr(ss, "игра", запись("игра_steam", None))
    monkeypatch.setattr(itad, "минимум", запись("минимум", None))
    взятый: list[jobs.Магазин] = []
    минимум: list = []

    async def день(магазин, учётка=None, минимум_=None):
        взятый.append(магазин)
        минимум.append(минимум_)
        return "день"

    monkeypatch.setattr(jobs, "день", день)
    assert asyncio.run(jobs.запустить("day", среда[0])) == "день"
    м = взятый[0]
    п = ss.Игра(номер=1, имя="и")

    async def все():
        await м.витрина(True, 2)
        await м.игра_eshop("о1")
        await м.найти_в_steam(п)
        await м.найти_в_eshop(п)
        await м.отзывы_eshop(п)
        await м.цены_eshop([ЕШОП])
        await м.найти(122, False, 1)
        await м.игра(620)
        await минимум[0](620)

    asyncio.run(все())
    assert вызовы == [
        ("витрина", ("eshop", регион), {"будущие": True, "страница": 2}),
        ("игра", ("eshop", регион, "о1"), {}),
        ("найти_в_steam", ("steam", п), {"страна_steam": страна_steam}),
        ("найти_в_eshop", ("eshop", регион, п), {}),
        ("отзывы", ("metacritic", п), {}),
        ("цены", ("eshop", регион, [ЕШОП]), {}),
        ("найти_с_отзывами", ("steam", 122), {"будущие": False, "страница": 1, "страна": страна_steam}),
        ("игра_steam", ("steam", 620), {"с_отзывами": False, "страна": страна_steam}),
        ("минимум", ("steam", "ключ-itad", 620), {"страна": страна_steam}),
    ]


def test_дата_минимума_местная(monkeypatch):
    """Отметка цены — по UTC; 22:30 UTC 5 сентября в Москве — уже 6 сентября."""
    import time as время
    monkeypatch.setattr(jobs, "_сегодня", lambda: "2026-10-19")
    monkeypatch.setenv("TZ", "Europe/Moscow")
    время.tzset()
    try:
        м = {"цена": 1499, "валюта": "USD", "когда": "2026-09-05T22:30:00+00:00", "с": ""}
        assert jobs._про_минимум_наблюдений(м, es.Цена(1999, 3999, 50, "USD", "")).endswith("(6 сентября)")
    finally:
        monkeypatch.undo()
        время.tzset()


def скидки_eshop_дня(с, цены, номера_игр=(ЕШОП,)):
    """Желаемое: игры eShop без пары; день с поддельной `цены_eshop`."""
    for н in номера_игр:
        games_db.запомнить_игру(с, игра_eshop(н))
        games_db.оценить(с, н, True, "")
    м = магазин({})
    return jobs.Магазин(найти=м.найти, игра=м.игра, цены_eshop=цены)


def test_первое_наблюдение_со_скидкой_без_минимума(среда, monkeypatch):
    tmp, уведомления, _ = среда
    monkeypatch.setattr(jobs, "_сегодня", lambda: "2026-10-19")
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра_eshop(ЕШОП + 1))
    games_db.оценить(с, ЕШОП + 1, True, "")
    with с:  # минимум в другой валюте с ценой не сравнить
        с.execute("INSERT INTO price_history (номер, цена, валюта, отмечено) VALUES (?, 100, 'EUR', '2026-09-01T00:00:00')", (ЕШОП + 1,))

    async def цены(номера):
        return {н: es.Цена(1999, 3999, 50, "USD", "2026-11-05") for н in номера}

    asyncio.run(jobs.день(скидки_eshop_дня(с, цены)))
    assert sorted(у for у in уведомления if у.startswith("Скидка")) == [
        f"Скидка 50 % на «игра {ЕШОП}» в eShop: 19.99 USD вместо 39.99 USD, до 5 ноября",
        f"Скидка 50 % на «игра {ЕШОП + 1}» в eShop: 19.99 USD вместо 39.99 USD, до 5 ноября",
    ]


def test_кривой_срок_скидки_не_теряет_скидки(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()

    async def цены(номера):
        return {ЕШОП: es.Цена(1999, 3999, 50, "USD", "скоро"), ЕШОП + 1: es.Цена(999, 1999, 50, "USD", "")}

    asyncio.run(jobs.день(скидки_eshop_дня(с, цены, (ЕШОП, ЕШОП + 1))))
    assert sorted(у for у in уведомления if у.startswith("Скидка")) == [
        f"Скидка 50 % на «игра {ЕШОП}» в eShop: 19.99 USD вместо 39.99 USD",
        f"Скидка 50 % на «игра {ЕШОП + 1}» в eShop: 9.99 USD вместо 19.99 USD",
    ]


def test_сбой_цен_eshop_скидки_steam_целы(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(7))
    games_db.оценить(с, 7, True, "")

    async def цены(номера):
        raise ss.ОшибкаМагазина("HTTP 503")

    м = скидки_eshop_дня(с, цены)
    м = jobs.Магазин(найти=м.найти, игра=магазин({7: игра(7, цена=50000, скидка=50)}).игра, цены_eshop=цены)
    asyncio.run(jobs.день(м))
    assert any(у.startswith("Скидка 50 % на «игра 7» в Steam") for у in уведомления)
    assert any(з["род"] == "сбой" and "eShop не ответил: HTTP 503" in з["текст"] for з in games_db.лента(с))


def test_скидки_после_сбоя_поиска_пары_не_ищут(среда):
    tmp, _, _ = среда
    учётка = jobs.Учётка(библиотека=lambda: asyncio.sleep(0, []), список_желаемого=lambda: asyncio.sleep(0, [8, 9]))
    вызовы: list[int] = []

    async def найти_в_eshop(и):
        вызовы.append(и.номер)
        raise ss.ОшибкаМагазина("HTTP 503")

    asyncio.run(jobs.день(магазин_с_парой({8: игра(8), 9: игра(9)}, найти_в_eshop), учётка))
    с = games_db.открыть()
    assert вызовы == [8]
    сбои = [з["текст"] for з in games_db.лента(с) if з["род"] == "сбой"]
    assert сбои == ["Проверка скидок не дошла до игр: поиск пары не удался: игра 8: HTTP 503"]


@pytest.mark.parametrize("отпечаток", ["А", jobs.НЕ_ИГРА])
def test_пара_с_отказом_модели_не_занята(среда, отпечаток):
    """Отказ модели версии eShop и «не игра» у пары — не решение человека: игра Steam
    предлагается."""
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.запомнить_отказ(с, ЕШОП, отпечаток)

    async def найти_в_eshop(и):
        return игра_eshop(имя=и.имя)

    итог = asyncio.run(jobs.неделя(tmp, магазин_с_парой({1: игра(1)}, найти_в_eshop), судья([])))
    assert итог.подошло == ["игра 1"] and games_db.пара(с, 1) == ЕШОП
    # Следующий запуск: пара уже известна — проверка до суда её тоже не считает занятой.
    games_db.снять_оценку(с, 1)
    судимые: list[int] = []
    asyncio.run(jobs.неделя(tmp, магазин_с_парой({1: игра(1)}, найти_в_eshop), судья(судимые)))
    assert судимые == [1]


class Счётчик:
    """Все поля eShop `Магазин` — со счётчиком вызовов на каждое."""

    def __init__(self):
        self.вызовы: list[str] = []

    def поле(self, имя, ответ):
        async def ф(*_):
            self.вызовы.append(имя)
            return ответ() if callable(ответ) else ответ
        return ф

    def поля(self):
        return dict(
            витрина=self.поле("витрина", lambda: [игра_eshop(ЕШОП + 5)] if self.вызовы.count("витрина") == 1 else []),
            игра_eshop=self.поле("игра_eshop", None),
            найти_в_steam=self.поле("найти_в_steam", None),
            найти_в_eshop=self.поле("найти_в_eshop", None),
            отзывы_eshop=self.поле("отзывы_eshop", ss.НЕТ_ОТЗЫВОВ),
            цены_eshop=self.поле("цены_eshop", {}),
        )


@pytest.mark.parametrize("вкл", [True, False])
def test_выключатель_eshop_неделя_и_день(среда, вкл):
    """Выключенный eShop — ни одного запроса к Nintendo и Metacritic: ни отбор недели, ни
    скидки дня не зовут ни одно поле eShop `Магазин`."""
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.включить_eshop(с, вкл)
    games_db.запомнить_игру(с, игра_eshop(ЕШОП))
    games_db.оценить(с, ЕШОП, True, "")
    games_db.запомнить_игру(с, игра(7))
    games_db.оценить(с, 7, True, "")
    счёт = Счётчик()
    м = магазин({1: игра(1), 7: игра(7)})
    м = jobs.Магазин(найти=м.найти, игра=м.игра, **счёт.поля())
    asyncio.run(jobs.неделя(tmp, м, судья([])))
    недели = list(счёт.вызовы)
    учётка = jobs.Учётка(библиотека=lambda: asyncio.sleep(0, []), список_желаемого=lambda: asyncio.sleep(0, [7]))
    asyncio.run(jobs.день(м, учётка))
    дня = счёт.вызовы[len(недели):]
    с.close()
    if вкл:
        assert "витрина" in недели and "найти_в_eshop" in недели
        assert "цены_eshop" in дня
    else:
        assert недели == [] and дня == []


def test_запустить_без_eshop_полей_не_ставит(среда, monkeypatch):
    """Выключенный eShop — в `Магазин` запуска поля eShop остаются пустыми, по умолчанию."""
    import contextlib

    import metacritic

    @contextlib.asynccontextmanager
    async def клиент():
        yield None

    for модуль in (ss, es, metacritic):
        monkeypatch.setattr(модуль, "клиент", клиент)
    с = games_db.открыть()
    games_db.включить_eshop(с, False)
    с.close()
    взятый: list[jobs.Магазин] = []

    async def день(магазин, учётка=None, минимум=None):
        взятый.append(магазин)
        return "день"

    monkeypatch.setattr(jobs, "день", день)
    assert asyncio.run(jobs.запустить("day", среда[0])) == "день"
    пустой = jobs.Магазин(найти=None, игра=None)
    м = взятый[0]
    assert (м.витрина, м.игра_eshop, м.найти_в_steam, м.найти_в_eshop, м.отзывы_eshop, м.цены_eshop) == (
        пустой.витрина, пустой.игра_eshop, пустой.найти_в_steam, пустой.найти_в_eshop,
        пустой.отзывы_eshop, пустой.цены_eshop)


def test_выключенный_eshop_скрытые_предложения_мест_не_занимают(среда):
    """Скрытые открытые предложения eShop не видны человеку — из десяти мест не вычитаются."""
    tmp, _, _ = среда
    с = games_db.открыть()
    try:
        for н in range(3):
            games_db.запомнить_игру(с, игра_eshop(ЕШОП + 20 + н))
            games_db.предложить(с, ЕШОП + 20 + н, "")
        for н in range(100, 107):
            games_db.запомнить_игру(с, игра(н))
            games_db.предложить(с, н, "")
        games_db.включить_eshop(с, False)
    finally:
        с.close()
    итог = asyncio.run(jobs.неделя(tmp, магазин({}), судья([])))
    assert итог.нужно == 3


def test_месяц_без_eshop_не_уточняет_игру_eshop_календаря(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    try:
        games_db.запомнить_игру(с, игра_eshop(ЕШОП, вышла=False))
        games_db.в_календарь(с, ЕШОП, "")
        games_db.включить_eshop(с, False)
    finally:
        с.close()
    счёт = Счётчик()
    м = jobs.Магазин(найти=lambda *_: asyncio.sleep(0, []), игра=lambda н: asyncio.sleep(0, None), **счёт.поля())
    asyncio.run(jobs.месяц(tmp, м, судья([])))
    assert счёт.вызовы == []


def test_выключенный_eshop_выход_игры_eshop_не_сообщается(среда):
    """Игра eShop выходит сегодня, eShop выключен — ни строки в ленте, ни уведомления, ни отметки;
    игра Steam в тот же день сообщается. Включение возвращает игру eShop в расчёт."""
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    try:
        games_db.запомнить_игру(с, игра_eshop(ЕШОП, вышла=False, дата=date.today(), имя="игра eShop"))
        games_db.в_календарь(с, ЕШОП, "")
        games_db.запомнить_игру(с, игра(8, вышла=False, дата=date.today()))
        games_db.в_календарь(с, 8, "")
        # Игра Steam с парой eShop — игра Steam: сообщается и при выключенном eShop.
        games_db.запомнить_игру(с, игра_eshop(ЕШОП + 1, вышла=False, дата=date.today(), имя="пара eShop"))
        games_db.записать_пару(с, 8, ЕШОП + 1)
        games_db.включить_eshop(с, False)
    finally:
        с.close()
    asyncio.run(jobs.день(магазин({})))
    с = games_db.открыть()
    try:
        assert [у for у in уведомления if у.startswith("Сегодня выходит")] == ["Сегодня выходит «игра 8»"]
        assert not any("игра eShop" in з["текст"] for з in games_db.лента(с))
        assert games_db.отметка(с, f"выход_сообщён:{ЕШОП}") is None
        games_db.включить_eshop(с, True)
    finally:
        с.close()
    asyncio.run(jobs.день(магазин({})))
    assert [у for у in уведомления if у.startswith("Сегодня выходит")] == [
        "Сегодня выходит «игра 8»", "Сегодня выходит «игра eShop»"]


def test_выключенный_eshop_скрытая_пара_не_блокирует_steam(среда):
    """Версия eShop показана и не оценена, eShop выключен — её не видно, игра Steam судится и
    предлагается."""
    tmp, _, _ = среда
    с = games_db.открыть()
    try:
        games_db.запомнить_игру(с, игра_eshop(ЕШОП))
        games_db.предложить(с, ЕШОП, "")
        games_db.запомнить_игру(с, игра(1))
        games_db.записать_пару(с, 1, ЕШОП)
        games_db.включить_eshop(с, False)
        судимые: list[int] = []
        итог = asyncio.run(jobs.неделя(tmp, магазин({1: игра(1)}), судья(судимые)))
        assert судимые == [1] and итог.подошло == ["игра 1"]
        assert 1 in [з["номер"] for з in games_db.предложения(с)]
    finally:
        с.close()


# ---- регионы ----


def _без_сети(monkeypatch):
    """Клиенты магазинов без сети и задания-подделки: проверяется только сам `запустить`."""
    import contextlib

    import metacritic

    @contextlib.asynccontextmanager
    async def клиент():
        yield None

    for модуль in (ss, eshop, metacritic):
        monkeypatch.setattr(модуль, "клиент", клиент)
    взятый: list[jobs.Магазин] = []

    async def день(магазин, учётка=None, минимум=None):
        взятый.append(магазин)
        return "день"

    monkeypatch.setattr(jobs, "день", день)
    return взятый


def test_без_страны_steam_задание_не_идёт(среда, monkeypatch):
    """Страны Steam нет — задание не идёт: одна строка в ленте на все запуски, без уведомления
    и без отметки сбоя; итог говорит, что делать. Страна появилась — отметка снята."""
    tmp, уведомления, _ = среда
    взятый = _без_сети(monkeypatch)
    с = games_db.открыть()
    с.execute("DELETE FROM marks WHERE ключ = 'страна_steam'")
    for _ in range(2):
        итог = asyncio.run(jobs.запустить("day", tmp))
        assert итог == "Задание «день» не выполнено: страна Steam не выбрана — укажите в настройках агента"
    assert взятый == [] and уведомления == []
    assert sum("Укажите страну Steam в настройках" in з["текст"] for з in games_db.лента(с)) == 1
    assert not games_db.отметка(с, "day_сбой")
    # Не начато — значок не перезапускает задание раз в минуту.
    assert not games_db.отметка(с, "day_начато")
    assert not any(jobs.нужен_догоняющий(с, р) for р in jobs.РОДЫ)
    games_db.задать_страны(с, steam="KZ")
    assert asyncio.run(jobs.запустить("day", tmp)) == "день"
    assert not games_db.отметка(с, "нет_страны_steam")
    с.close()


@pytest.mark.parametrize("страна", [None, "RU"])
def test_без_страны_eshop_ветви_закрыты(среда, monkeypatch, страна):
    """Страны eShop нет или eShop в ней не поддержан — поля eShop пусты, ветви заданий закрыты,
    хотя выключатель включён."""
    tmp, _, _ = среда
    взятый = _без_сети(monkeypatch)
    с = games_db.открыть()
    с.execute("DELETE FROM marks WHERE ключ = 'страна_eshop'")
    if страна:
        games_db.задать_страны(с, eshop=страна)
    assert games_db.eshop_включён(с) and not jobs._ходить_в_eshop(с)
    assert asyncio.run(jobs.запустить("day", tmp)) == "день"
    пустой = jobs.Магазин(найти=None, игра=None)
    assert взятый[0].витрина is пустой.витрина and взятый[0].цены_eshop is пустой.цены_eshop
    счёт = Счётчик()
    м = магазин({1: игра(1)})
    asyncio.run(jobs.неделя(tmp, jobs.Магазин(найти=м.найти, игра=м.игра, **счёт.поля()), судья([])))
    assert счёт.вызовы == []
    с.close()


def test_кандидат_eshop_с_запрещённым_именем_не_судится(среда, monkeypatch):
    """Запрет человека по имени переживает смену зоны eShop: номер в новой зоне другой."""
    tmp, _, _ = среда
    monkeypatch.setattr(taste, "запреты_по_имени", lambda к: {es.сравнимое("Hollow Knight")})
    е = Ешоп([[игра_eshop(ЕШОП, имя="Hollow Knight™"), игра_eshop(ЕШОП + 1, имя="Celeste")]])
    м = магазин({})
    м = jobs.Магазин(найти=м.найти, игра=м.игра, **е.поля())
    судимые: list[int] = []
    asyncio.run(jobs.неделя(tmp, м, судья(судимые)))
    assert судимые == [ЕШОП + 1]
    assert ЕШОП not in е.в_steam  # и пара запрещённой не ищется


@pytest.mark.parametrize("сотые, валюта, текст", [
    (55000, "RUB", "550 руб."),
    (410000, "KZT", "4 100 ₸"),
    (1999, "USD", "19.99 USD"),
    (2000, "USD", "20 USD"),
    (1999, "EUR", "19.99 €"),
    (123456, "GBP", "1 234.56 £"),
    (100000, "RUB", "1 000 руб."),
    (99900, "BRL", "999 BRL"),
    (1999, "", "19.99"),
    (410000, "", "4 100"),
])
def test_сумма_в_валюте_страны(сотые, валюта, текст):
    assert jobs._сумма(сотые, валюта) == текст


def test_минимум_не_сравнивается_с_ценой_без_валюты():
    """Валюта цены не известна — с минимумом в рублях не сравнивать: прежний `or "RUB"` был
    зашитой страной."""
    async def минимум(номер):
        return SimpleNamespace(цена=30000, валюта="RUB", когда="2026-06-01T00:00:00Z")

    без_валюты = replace(игра(7, цена=20000), валюта="")
    assert asyncio.run(jobs._про_минимум(минимум, без_валюты)) == ""
    assert asyncio.run(jobs._про_минимум(минимум, игра(7, цена=20000))) == "; это исторический минимум"


def test_запрет_по_имени_не_касается_steam(среда, monkeypatch):
    """Запрет по имени — только для кандидатов eShop: игра Steam держится запретом по номеру."""
    tmp, _, _ = среда
    monkeypatch.setattr(taste, "запреты_по_имени", lambda к: {es.сравнимое("игра 1")})
    судимые: list[int] = []
    asyncio.run(jobs.неделя(tmp, магазин({1: игра(1)}), судья(судимые)))
    assert судимые == [1]


# ---- задание «регион» ----


def _пересборка(monkeypatch, во_время=None):
    """Подделка `regions.пересобрать`: доводы вызова и взятые магазины; сеть не нужна."""
    import regions

    вызовы: list[dict] = []

    async def пересобрать(с, магазины, *, steam_сменилась, eshop, страны):
        вызовы.append({"steam_сменилась": steam_сменилась, "eshop": eshop, "страны": страны,
                       "магазины": магазины})
        if во_время:
            во_время(с)
        games_db.задать_страны(с, **страны)
        return regions.Итог(обновлено=["игра"])

    monkeypatch.setattr(regions, "пересобрать", пересобрать)
    return вызовы


def test_регион_первая_установка_только_страны(среда, monkeypatch):
    tmp, уведомления, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    с = games_db.открыть()
    с.execute("DELETE FROM marks WHERE ключ IN ('страна_steam', 'страна_eshop')")
    итог = asyncio.run(jobs.запустить("region", tmp, новые={"steam": "DE", "eshop": "GB"}))
    assert вызовы == [] and уведомления == []
    assert (games_db.страна_steam(с), games_db.страна_eshop(с)) == ("DE", "GB")
    assert games_db.eshop_включён(с) and итог == "Регион задан: Steam DE, eShop GB"
    с.close()


@pytest.mark.parametrize("новые, прежняя_eshop, ждём", [
    ({"steam": "DE", "eshop": "US"}, "US", (True, "без_изменений")),
    ({"steam": "KZ", "eshop": "CA"}, "US", (False, "та_же_зона")),
    ({"steam": "KZ", "eshop": "GB"}, "US", (False, "другая_зона")),
    ({"steam": "DE", "eshop": "GB"}, "US", (True, "другая_зона")),
    ({"steam": "DE", "eshop": None}, "US", (True, "выключен")),
    # Страны eShop не было: игр eShop нет, пары игр Steam ищутся впервые.
    ({"steam": "KZ", "eshop": "US"}, None, (False, "другая_зона")),
])
def test_регион_доводы_пересборки(среда, monkeypatch, новые, прежняя_eshop, ждём):
    tmp, _, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    с = games_db.открыть()
    if прежняя_eshop is None:
        с.execute("DELETE FROM marks WHERE ключ = 'страна_eshop'")
    итог = asyncio.run(jobs.запустить("region", tmp, новые=новые))
    assert len(вызовы) == 1 and (вызовы[0]["steam_сменилась"], вызовы[0]["eshop"]) == ждём
    страны = {"steam": новые["steam"], **({"eshop": новые["eshop"]} if новые["eshop"] else {})}
    assert вызовы[0]["страны"] == страны
    assert итог.startswith("Регион сменён: обновлено 1")
    # Страна eShop — вместе с выключателем: `null` выключает eShop, страна — включает.
    assert games_db.eshop_включён(с) is (новые["eshop"] is not None)
    с.close()


@pytest.mark.parametrize("новые, вкл", [({"steam": "KZ", "eshop": "US"}, True),
                                         ({"steam": "KZ", "eshop": None}, False)])
def test_регион_без_перемен_без_пересборки(среда, monkeypatch, новые, вкл):
    tmp, _, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    с = games_db.открыть()
    games_db.включить_eshop(с, not вкл)
    итог = asyncio.run(jobs.запустить("region", tmp, новые=новые))
    assert вызовы == [] and итог.startswith("Регион прежний")
    assert games_db.eshop_включён(с) is вкл
    assert (games_db.страна_steam(с), games_db.страна_eshop(с)) == ("KZ", "US")
    с.close()


def test_регион_выключенный_eshop_с_новой_страной_пересобирает(среда, monkeypatch):
    """eShop был выключен, человек выбрал страну eShop: выключатель включается, игры eShop,
    лежавшие скрытыми под прежней страной, пересобираются — иначе всплыли бы чужой зоны."""
    tmp, _, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    с = games_db.открыть()
    games_db.включить_eshop(с, False)
    asyncio.run(jobs.запустить("region", tmp, новые={"steam": "KZ", "eshop": "GB"}))
    assert вызовы[0]["eshop"] == "другая_зона" and games_db.eshop_включён(с)
    с.close()


def test_регион_связывает_магазины(среда, monkeypatch):
    """Поля `regions.Магазины` — функции `steam_store` и `eshop` новой страны; `прежняя_eshop` —
    каталог прежней страны eShop."""
    tmp, _, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    взятые: list[tuple] = []

    def запись(имя, ответ):
        async def ф(*доводы, **поля):
            взятые.append((имя, доводы, поля))
            return ответ
        return ф

    monkeypatch.setattr(ss, "игра", запись("игра_steam", None))
    monkeypatch.setattr(eshop, "игра", запись("игра", None))
    monkeypatch.setattr(eshop, "найти_по_названию", запись("найти_по_названию", []))
    monkeypatch.setattr(eshop, "найти_в_eshop", запись("найти_в_eshop", None))
    asyncio.run(jobs.запустить("region", tmp, новые={"steam": "DE", "eshop": "GB"}))
    м = вызовы[0]["магазины"]
    п = ss.Игра(номер=1, имя="и")

    async def все():
        await м.игра_steam(620)
        await м.игра_eshop("о1")
        await м.найти_eshop("Игра")
        await м.прежняя_eshop("о2")
        await м.пара_eshop(п)

    asyncio.run(все())
    assert взятые == [
        ("игра_steam", (None, 620), {"с_отзывами": False, "страна": "DE"}),
        ("игра", (None, eshop.регион("GB"), "о1"), {}),
        ("найти_по_названию", (None, eshop.регион("GB"), "Игра"), {}),
        ("игра", (None, eshop.регион("US"), "о2"), {}),
        ("найти_в_eshop", (None, eshop.регион("GB"), п), {}),
    ]


def test_регион_ход_на_странице(среда, monkeypatch):
    tmp, _, _ = среда
    _без_сети(monkeypatch)
    ходы: list = []
    _пересборка(monkeypatch, во_время=lambda с: ходы.append(jobs.ход_задания(с, "region")))
    asyncio.run(jobs.запустить("region", tmp, новые={"steam": "DE", "eshop": "US"}))
    assert ходы[0]["этап"] == "пересобираю данные под новый регион"
    с = games_db.открыть()
    assert jobs.ход_задания(с, "region") is None
    с.close()


@pytest.mark.parametrize("род", ["day", "week", "month"])
def test_регион_не_идёт_вместе_с_заданиями(среда, monkeypatch, род):
    """Пересборка и отбор пишут одни таблицы: идёт любое задание — «регион» не начинается, идёт
    «регион» — не начинается ни одно. Догоняющего запуска во время пересборки нет."""
    tmp, _, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    замок = jobs._замок(род)
    try:
        with pytest.raises(jobs.УжеИдёт):
            asyncio.run(jobs.запустить("region", tmp, новые={"steam": "DE", "eshop": "US"}))
    finally:
        замок.close()
    assert вызовы == []
    с = games_db.открыть()
    assert games_db.страна_steam(с) == "KZ"
    замок = jobs._замок("region")
    try:
        with pytest.raises(jobs.УжеИдёт):
            asyncio.run(jobs.запустить(род, tmp))
        assert not jobs.нужен_догоняющий(с, род)
        # Замок рода не занят пересборкой: на странице задание рода не «идёт».
        assert jobs.ход_задания(с, род) is None
    finally:
        замок.close()
    с.close()


def test_регион_не_по_расписанию(среда):
    с = games_db.открыть()
    assert not jobs.нужен_догоняющий(с, "region")
    с.close()
    assert "region" in jobs.РОДЫ and jobs.ИМЕНА["region"] == "регион"
    assert "region" not in jobs.ПО_РАСПИСАНИЮ
    assert jobs.main(["region"]) == 2


@pytest.mark.parametrize("новые", [{"steam": "kz", "eshop": None}, {"steam": "KZ", "eshop": "RU"},
                                   {"steam": "KZ"}, {"eshop": "US"}])
def test_регион_неверные_страны(среда, monkeypatch, новые):
    """Неверные страны — `ValueError` до замка: замок не взят, отметок и строк ленты нет."""
    tmp, уведомления, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    замки: list[str] = []
    прежний = jobs._замок
    monkeypatch.setattr(jobs, "_замок", lambda род: (замки.append(род), прежний(род))[1])
    with pytest.raises(ValueError):
        asyncio.run(jobs.запустить("region", tmp, новые=новые))
    assert вызовы == [] and уведомления == [] and замки == []
    с = games_db.открыть()
    assert not any(games_db.отметка(с, f"region_{к}") for к in ("начато", "выполнено", "сбой"))
    assert games_db.лента(с) == []
    с.close()


@pytest.mark.parametrize("прежние, новые, ждём", [
    ((None, None), ("KZ", "US"), None),
    (("KZ", "US"), ("KZ", "US"), None),
    (("KZ", "US"), ("KZ", None), None),
    (("KZ", None), ("KZ", None), None),
    (("KZ", "US"), ("DE", "US"), (True, "без_изменений")),
    (("KZ", "US"), ("DE", None), (True, "выключен")),
    (("KZ", "US"), ("KZ", "CA"), (False, "та_же_зона")),
    (("KZ", "US"), ("KZ", "GB"), (False, "другая_зона")),
    (("KZ", None), ("KZ", "US"), (False, "другая_зона")),
])
def test_смена_региона(прежние, новые, ждём):
    """Чистая функция ветвей: `None` — пересборки нет (первая установка или без перемен)."""
    assert jobs.смена_региона(*прежние, *новые) == ждём


def test_своё_задание_дожидается_пробы(среда, monkeypatch):
    """Проба чужого замка держит его миг: своё задание ждёт замок до `ОЖИДАНИЕ_ЗАМКА_С`, а не
    пропускает запуск."""
    import threading

    tmp, _, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    assert jobs.ОЖИДАНИЕ_ЗАМКА_С == 1.0
    проба = jobs._замок("region")
    threading.Timer(0.1, проба.close).start()
    asyncio.run(jobs.запустить("region", tmp, новые={"steam": "DE", "eshop": "US"}))
    assert len(вызовы) == 1


def test_замок_занят_дольше_ожидания(среда, monkeypatch):
    tmp, _, _ = среда
    _без_сети(monkeypatch)
    monkeypatch.setattr(jobs, "ОЖИДАНИЕ_ЗАМКА_С", 0.05)
    замок = jobs._замок("region")
    try:
        with pytest.raises(jobs.УжеИдёт):
            asyncio.run(jobs.запустить("region", tmp, новые={"steam": "DE", "eshop": "US"}))
    finally:
        замок.close()


@pytest.mark.parametrize("род", ["region", "day"])
def test_чужие_замки_пробуются_при_своём(среда, monkeypatch, род):
    """Порядок замков: при пробе чужих свой замок уже взят — иначе обе стороны прошли бы."""
    tmp, _, _ = среда
    _без_сети(monkeypatch)
    _пересборка(monkeypatch)
    своё_взято: list[bool] = []

    async def не_вместе(р):
        try:
            jobs._замок(р).close()
            своё_взято.append(False)
        except jobs.УжеИдёт:
            своё_взято.append(True)

    monkeypatch.setattr(jobs, "_не_вместе", не_вместе)
    новые = {"новые": {"steam": "DE", "eshop": "US"}} if род == "region" else {}
    asyncio.run(jobs.запустить(род, tmp, **новые))
    assert своё_взято == [True]


def test_служба_пропускает_из_за_региона(среда, monkeypatch):
    """Пропуск по расписанию из-за пересборки — строка называет её, а не «запущено кнопкой»."""
    _без_сети(monkeypatch)
    замок = jobs._замок("region")
    try:
        assert jobs.main(["day"]) == 0
    finally:
        замок.close()
    с = games_db.открыть()
    текст = games_db.лента(с)[0]["текст"]
    с.close()
    assert "идёт пересборка региона" in текст and "кнопкой" not in текст


def test_проба_чужого_замка_ждёт(среда, monkeypatch):
    """Чужой замок занят пробой страницы на миг — «регион» дожидается, а не отвечает ложной
    причиной «идёт задание»."""
    import threading

    tmp, _, _ = среда
    _без_сети(monkeypatch)
    вызовы = _пересборка(monkeypatch)
    проба = jobs._замок("week")
    threading.Timer(0.1, проба.close).start()
    asyncio.run(jobs.запустить("region", tmp, новые={"steam": "DE", "eshop": "US"}))
    assert len(вызовы) == 1


# ---- суд пачками (шаг 52) ----


def судьи(пачки: list[list[int]], одна: list[int], *, подходит=lambda и: True, сбои: list | None = None):
    """Судьи-подделки: пачка записывает номера каждой пачки и отвечает по `подходит`; `сбои` —
    исключения по очереди вызовов пачки (`None` — удачный ответ)."""
    очередь = list(сбои or [])

    async def пачка(игры):
        пачки.append([и.номер for и in игры])
        if очередь and (сбой := очередь.pop(0)) is not None:
            raise сбой
        return {и.номер: judge.Вердикт(подходит(и), f"по вкусу {и.номер}") for и in игры}

    return jobs.Судьи(одна=судья(одна, подходит), пачка=пачка)


def _этапы(monkeypatch) -> list[str]:
    этапы: list[str] = []
    прежний = jobs.ход

    def ход(с, род, **поля):
        этапы.append(поля.get("этап"))
        прежний(с, род, **поля)

    monkeypatch.setattr(jobs, "ход", ход)
    return этапы


def test_пачка_удачна_лишние_сверх_нужного_не_записаны(среда, monkeypatch):
    tmp, _, _ = среда
    monkeypatch.setattr(jobs, "НУЖНО", 2)
    пачки: list[list[int]] = []
    одна: list[int] = []
    игры = {н: игра(н) for н in (1, 3, 4, 5, 6)}
    итог = asyncio.run(jobs.неделя(tmp, магазин(игры), судьи(пачки, одна, подходит=lambda и: и.номер in (1, 3, 5))))
    с = games_db.открыть()
    assert пачки == [[1, 3, 4, 5, 6]] and одна == []
    assert {з["номер"] for з in games_db.предложения(с)} == {1, 3}
    # Одобренная 5 сверх «нужно» — ни предложением, ни отказом; отвергнутые 4 и 6 — отказы.
    assert games_db.отказано(с, "А") == {4, 6}
    assert итог.подошло == ["игра 1", "игра 3"] and итог.судимо == 5
    assert "запасном" not in итог.текст("неделя")


def test_пачка_с_ошибкой_повтор_затем_по_одной(среда):
    tmp, _, _ = среда
    пачки: list[list[int]] = []
    одна: list[int] = []
    сбои = [ValueError("нет ответа по номерам 3"), RuntimeError("местная модель не ответила")]
    итог = asyncio.run(jobs.неделя(tmp, магазин({н: игра(н) for н in (1, 3, 4)}), судьи(пачки, одна, сбои=сбои)))
    assert пачки == [[1, 3, 4], [1, 3, 4]] and одна == [1, 3, 4]
    assert итог.подошло == ["игра 1", "игра 3", "игра 4"] and итог.судимо == 3
    текст = итог.текст("неделя")
    assert "пачек на запасном пути: 1" in текст and "нет ответа по номерам 3" in текст


def test_пачка_удачна_со_второго_раза(среда):
    tmp, _, _ = среда
    пачки: list[list[int]] = []
    одна: list[int] = []
    итог = asyncio.run(jobs.неделя(tmp, магазин({1: игра(1)}), судьи(пачки, одна, сбои=[RuntimeError("сбой")])))
    assert пачки == [[1], [1]] and одна == [] and итог.подошло == ["игра 1"]
    assert "запасном" not in итог.текст("неделя")


def test_пачки_в_порядке_кандидатов_и_неполная_последняя(среда, monkeypatch):
    tmp, _, _ = среда
    этапы = _этапы(monkeypatch)
    пачки: list[list[int]] = []
    одна: list[int] = []
    # Оценки по убыванию: 9, 8, …, 1 — порядок кандидатов от номера к номеру не совпадает.
    доли = {11: 0.5, 12: 0.9, 13: 0.6, 14: 0.8, 15: 0.55, 16: 0.7, 17: 0.65}
    игры = {н: игра(н, доля=д) for н, д in доли.items()}
    итог = asyncio.run(jobs.неделя(tmp, магазин(игры), судьи(пачки, одна, подходит=lambda и: False)))
    assert пачки == [[12, 14, 16, 17, 13], [15, 11]] and одна == []
    assert итог.судимо == 7 and итог.подошло == []
    assert [э for э in этапы if э and э.startswith("сужу")] == ["сужу пачку 1", "сужу пачку 2"]


def test_пачка_не_набирается_после_нужного(среда, monkeypatch):
    """Нужное набрано первой пачкой — вторая не судится."""
    tmp, _, _ = среда
    monkeypatch.setattr(jobs, "НУЖНО", 2)
    пачки: list[list[int]] = []
    игры = {н: игра(н) for н in (1, 3, 4, 5, 6, 7, 8)}
    asyncio.run(jobs.неделя(tmp, магазин(игры), судьи(пачки, [])))
    assert пачки == [[1, 3, 4, 5, 6]]


def test_запасной_путь_останавливается_на_нужном(среда, monkeypatch):
    """Запасной путь судит по одной до «нужно»: игры после него не судятся и отказом не пишутся."""
    tmp, _, _ = среда
    monkeypatch.setattr(jobs, "НУЖНО", 1)
    пачки: list[list[int]] = []
    одна: list[int] = []
    сбои = [ValueError("а"), ValueError("б")]
    игры = {н: игра(н) for н in (1, 3, 4, 5)}
    итог = asyncio.run(jobs.неделя(tmp, магазин(игры), судьи(пачки, одна, подходит=lambda и: и.номер == 3, сбои=сбои)))
    с = games_db.открыть()
    assert одна == [1, 3] and итог.подошло == ["игра 3"]
    assert games_db.отказано(с, "А") == {1}


@pytest.mark.parametrize("вердикт_eshop", [True, False])
def test_пачка_игра_steam_и_её_пара_eshop(среда, вердикт_eshop):
    """Игра Steam и её версия eShop в одной пачке: Steam подошла и нашла пару — вердикт версии
    eShop не применяется: ни второй плашки, ни отказа, ни второго поиска пары."""
    tmp, _, _ = среда
    е = Ешоп([[игра_eshop(ЕШОП, имя="Общая")]])
    м = магазин({1: игра(1)})
    искали: list[int] = []

    async def найти_в_eshop(и):
        искали.append(и.номер)
        return игра_eshop(имя="Общая")

    м = jobs.Магазин(найти=м.найти, игра=м.игра, найти_в_eshop=найти_в_eshop, **е.поля())
    пачки: list[list[int]] = []
    итог = asyncio.run(jobs.неделя(tmp, м, судьи(пачки, [], подходит=lambda и: и.номер == 1 or вердикт_eshop)))
    с = games_db.открыть()
    assert пачки == [[1, ЕШОП]] and искали == [1]
    assert [з["номер"] for з in games_db.предложения(с)] == [1] and итог.подошло == ["игра 1"]
    assert games_db.отказано(с, "А") == set()


def test_инструменты_mcp_не_поднялись_пачки_больше_не_пробуются(среда):
    """Сервер инструментов не поднялся — повтор не поможет: пачка без повтора, до конца запуска
    по одной, одна строка в итоге."""
    tmp, _, _ = среда
    пачки: list[list[int]] = []
    одна: list[int] = []
    # Тип, а не текст: причина любая.
    причина = "сервер упал"
    игры = {н: игра(н) for н in range(10, 17)}
    итог = asyncio.run(jobs.неделя(tmp, магазин(игры), судьи(пачки, одна, сбои=[judge.НетИнструментов(причина)])))
    assert пачки == [[10, 11, 12, 13, 14]] and одна == list(range(10, 17))
    текст = итог.текст("неделя")
    assert f"суд пачками выключен на этот запуск: {причина}" in текст and "пачек на запасном пути" not in текст


def test_запустить_судит_пачкой_через_одни_соединения(среда, monkeypatch):
    """Неделя из `запустить`: соединения MCP — одни на задание и закрыты в конце; каталог суда —
    каталог данных агента; сбой RAG — похожих нет."""
    import agent_env
    import secrets_store
    import similar

    _без_сети(monkeypatch)
    monkeypatch.setattr(secrets_store, "загрузить", lambda: secrets_store.Тайны())
    созданы: list = []

    class Соединения:
        def __init__(self):
            self.закрыто = False
            созданы.append(self)

        async def закрыть(self):
            self.закрыто = True

    class Индекс:
        def похожие(self, игра):
            raise OSError("Ollama не отвечает")

    monkeypatch.setattr(jobs, "Соединения", Соединения)
    monkeypatch.setattr(similar, "Индекс", Индекс)
    вызовы: list[tuple] = []

    async def судить_пачкой(клиент, каталог, игры, похожие, *, соединения=None):
        вызовы.append((каталог, [и.номер for и in игры], похожие, соединения, соединения.закрыто))
        return {и.номер: judge.Вердикт(True, "да") for и in игры}

    monkeypatch.setattr(judge, "судить_пачкой", судить_пачкой)

    async def неделя(каталог, магазин, судьи, индекс=None):
        await судьи.пачка([игра(1), игра(3)])
        await судьи.пачка([игра(4)])
        return jobs.Итог(нужно=1)

    monkeypatch.setattr(jobs, "неделя", неделя)
    asyncio.run(jobs.запустить("week"))
    assert len(созданы) == 1 and созданы[0].закрыто
    assert [в[:3] for в in вызовы] == [
        (agent_env.данные(), [1, 3], {1: "", 3: ""}),
        (agent_env.данные(), [4], {4: ""}),
    ]
    assert all(в[3] is созданы[0] and not в[4] for в in вызовы)


def test_запустить_закрывает_соединения_при_сбое(среда, monkeypatch):
    import secrets_store
    import similar

    _без_сети(monkeypatch)
    monkeypatch.setattr(secrets_store, "загрузить", lambda: secrets_store.Тайны())
    созданы: list = []

    class Соединения:
        def __init__(self):
            self.закрыто = False
            созданы.append(self)

        async def закрыть(self):
            self.закрыто = True

    monkeypatch.setattr(jobs, "Соединения", Соединения)
    monkeypatch.setattr(similar, "Индекс", lambda: None)

    async def неделя(*_, **__):
        raise RuntimeError("задание упало")

    monkeypatch.setattr(jobs, "неделя", неделя)
    with pytest.raises(RuntimeError, match="задание упало"):
        asyncio.run(jobs.запустить("week"))
    assert len(созданы) == 1 and созданы[0].закрыто


def test_запустить_снимает_замок_если_закрытие_соединений_упало(среда, monkeypatch):
    import secrets_store

    _без_сети(monkeypatch)
    monkeypatch.setattr(secrets_store, "загрузить", lambda: secrets_store.Тайны())

    class Соединения:
        async def закрыть(self):
            raise OSError("сервер не закрылся")

    monkeypatch.setattr(jobs, "Соединения", Соединения)
    with pytest.raises(OSError):
        asyncio.run(jobs.запустить("day"))
    jobs._замок("day").close()  # замок свободен: `УжеИдёт` не поднимается
