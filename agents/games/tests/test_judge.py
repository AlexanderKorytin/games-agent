import pytest

import judge


def test_разбор_ответа():
    assert judge.разобрать('{"подходит": true, "причина": "  пошаговые  бои "}') == judge.Вердикт(True, "пошаговые бои")
    with pytest.raises(ValueError, match="не JSON"):
        judge.разобрать("да, подходит")
    with pytest.raises(ValueError, match="подходит"):
        judge.разобрать('{"причина": "x"}')


def test_запрос_подписывает_магазин_и_оценку(каталоги):
    from myharness.profiles import builtin_default
    from steam_store import Игра, Отзывы

    профиль = builtin_default()
    отзывы = Отзывы("Очень положительные", 90, 100)
    eshop = judge.запрос(каталоги, Игра(70010000003208, "Super Mario Odyssey", отзывы=отзывы), профиль)
    assert "Магазин: Nintendo eShop" in eshop
    assert f"Оценка игроков Metacritic: {отзывы.текст()}" in eshop
    assert "Steam" not in eshop
    steam = judge.запрос(каталоги, Игра(367520, "Hollow Knight", отзывы=отзывы), профиль)
    assert "Магазин: Steam" in steam
    assert f"Оценка пользователей Steam: {отзывы.текст()}" in steam
    assert "Metacritic" not in steam and "eShop" not in steam


# --- суд пачкой через MCP -------------------------------------------------------------------

import asyncio
import json
from pathlib import Path

from myharness import api
from myharness.agent import Инструменты

ПРОФИЛИ = Path(judge.__file__).resolve().parent / "profiles"
STEAM, ESHOP = "mcp__steam__steam_game", "mcp__steam__eshop_game"


def _схема(имя, довод):
    return {"type": "function", "function": {"name": имя, "description": имя, "parameters": {
        "type": "object", "properties": {довод: {"type": "integer"}}, "required": [довод]}}}


class ПоддельныеСоединения:
    """Набор MCP без серверов: три инструмента (лишний `steam_search` — проверка сужения),
    исполнитель пишет вызовы. Номер из `сбои` отвечает ошибкой инструмента."""

    def __init__(self, сбои=()):
        self.каталоги, self.вызовы, self.сбои = [], [], set(сбои)

    async def набор(self, каталог):
        self.каталоги.append(каталог)

        async def исполнить(имя, доводы):
            д = json.loads(доводы)
            self.вызовы.append((имя, д))
            номер = д.get("appid") or д.get("nsuid")
            if номер in self.сбои:
                raise RuntimeError("магазин не ответил")
            return json.dumps({"номер": номер, "жанры": ["Ролевая игра"]})

        return Инструменты(
            [_схема(STEAM, "appid"), _схема(ESHOP, "nsuid"), _схема("mcp__steam__steam_search", "tag")],
            исполнить,
        )


def вызов(номер, eshop=False, i=0):
    имя, довод = (ESHOP, "nsuid") if eshop else (STEAM, "appid")
    return {"id": f"c{номер}{i}", "type": "function",
            "function": {"name": имя, "arguments": json.dumps({довод: номер})}}


class ПоддельнаяМодель:
    """Клиент модели по сценарию: каждый круг — список вызовов либо итоговый текст."""

    def __init__(self, круги):
        self.круги, self.запросы, self.инструменты = list(круги), [], []

    async def stream_chat(self, model, messages, params=None, tools=None):
        self.запросы.append(messages)
        self.инструменты.append([с["function"]["name"] for с in tools or []])
        круг = self.круги.pop(0)
        if isinstance(круг, list):
            yield api.StreamEvent("tool_calls", calls=круг)
        else:
            yield api.StreamEvent("content", круг)
        yield api.StreamEvent("meta", finish_reason="stop", usage={})



def _игры():
    from steam_store import Игра

    return [Игра(367520, "Hollow Knight"), Игра(70010000003208, "Super Mario Odyssey"), Игра(1145360, "Hades")]


def _итог(*пункты):
    return json.dumps([{"номер": н, "подходит": п, "причина": "  похожа на  Baldur's Gate 3 "} for н, п in пункты],
                      ensure_ascii=False)


ВСЕ_ВЫЗОВЫ = [[вызов(367520)], [вызов(70010000003208, eshop=True)], [вызов(1145360)]]


@pytest.fixture
def среда(каталоги, monkeypatch):
    monkeypatch.setenv("MYHARNESS_PROFILES", str(ПРОФИЛИ))
    monkeypatch.setenv("GAMES_LOCAL_MODEL", "ollama/qwen3.5:9b-q4_K_M-32k")
    return каталоги


def судить(среда, модель, соединения=None, похожие=None):
    соединения = соединения or ПоддельныеСоединения()
    return asyncio.run(judge.судить_пачкой(модель, среда, _игры(), похожие or {}, соединения=соединения))


def test_пачка_удачный_ответ(среда):
    import taste

    модель = ПоддельнаяМодель([*ВСЕ_ВЫЗОВЫ, "Готово:\n```json\n"
                               + _итог((367520, True), (70010000003208, False), (1145360, True)) + "\n```"])
    соединения = ПоддельныеСоединения()
    похожие = {367520: "## Похожие игры из истории человека\n- Ori — минус (сходство 0.91)"}
    from steam_store import Метка

    записано = asyncio.run(taste.набор(среда, [Метка(122, "Ролевая игра")]).исполнить(
        taste.СОХРАНИТЬ_ВКУС, json.dumps({"разделы": {
            "Любимые игры": ["Baldur's Gate 3"], "Общее": ["любит сюжет"],
            "Критерии": ["Ролевая игра (122) — 5"], "Нежелательное": ["хорроры"]}}, ensure_ascii=False)))
    assert записано.startswith("вкус записан")
    вердикты = asyncio.run(judge.судить_пачкой(модель, среда, _игры(), похожие, соединения=соединения))
    assert вердикты == {
        367520: judge.Вердикт(True, "похожа на Baldur's Gate 3"),
        70010000003208: judge.Вердикт(False, "похожа на Baldur's Gate 3"),
        1145360: judge.Вердикт(True, "похожа на Baldur's Gate 3"),
    }
    # Модели предложены только два инструмента сведений, лишний набора отсечён.
    assert all(sorted(и) == sorted([STEAM, ESHOP]) for и in модель.инструменты)
    запрос = модель.запросы[0][-1]["content"]
    assert "любит сюжет" in запрос and "хорроры" in запрос
    assert "367520 — Steam — Hollow Knight" in запрос
    assert "70010000003208 — Nintendo eShop — Super Mario Odyssey" in запрос
    assert "Ori — минус" in запрос and "## Похожие игры из истории человека" not in запрос
    # Шаги и формат ответа — только в профиле, полными именами инструментов; запрос — данные.
    система = модель.запросы[0][0]["content"]
    assert STEAM in система and ESHOP in система and "JSON-списком" in система
    assert "steam_game" not in запрос and "JSON" not in запрос


def test_пачка_каталог_инструментов_свой(среда):
    """Набор берётся из своего каталога задания: разрешены только два инструмента, сервер тот
    же, что у советника, а файл разрешений советника не тронут."""
    import advisor

    advisor.подготовить_каталог(среда)
    до = (среда / ".claude" / "settings.json").read_text("utf-8")
    модель = ПоддельнаяМодель([*ВСЕ_ВЫЗОВЫ, _итог((367520, True), (70010000003208, False), (1145360, True))])
    соединения = ПоддельныеСоединения()
    судить(среда, модель, соединения)
    свой = соединения.каталоги[0]
    assert свой != среда
    разрешено = json.loads((свой / ".claude" / "settings.json").read_text("utf-8"))["permissions"]["allow"]
    assert sorted(разрешено) == sorted([STEAM, ESHOP])
    assert json.loads((свой / ".mcp.json").read_text("utf-8")) == json.loads((среда / ".mcp.json").read_text("utf-8"))
    assert (среда / ".claude" / "settings.json").read_text("utf-8") == до


@pytest.mark.parametrize("итог, причина", [
    (_итог((367520, True), (1145360, True)), "нет ответа"),
    (_итог((367520, True), (70010000003208, False), (1145360, True), (620, True)), "не из пачки"),
    (_итог((367520, True), (70010000003208, False), (1145360, True), (1145360, False)), "дважды"),
    (_итог((367520, True), (70010000003208, False), ("1.97e+06", True)), "не целое"),
    (_итог((367520, True), (70010000003208, False), (1.97e+06, True)), "не целое"),
    (_итог((367520, True), (70010000003208, False), (1145360, "да")), "подходит"),
    ("Итак, я проанализировал игры: все подходят.", "не JSON"),
    ('{"номер": 367520, "подходит": true}', "не JSON-список"),
    ('[{"номер": 367520, ', "не JSON"),
    (json.dumps([{"номер": 367520, "подходит": True, "причина": "пошаговые бои"},
                 {"номер": 70010000003208, "подходит": False, "причина": "  "},
                 {"номер": 1145360, "подходит": True, "причина": "сюжет"}]), "нет причины у номера 70010000003208"),
    (json.dumps([{"номер": 367520, "подходит": True, "причина": "пошаговые бои"},
                 {"номер": 70010000003208, "подходит": False, "причина": "сюжет"},
                 {"номер": 1145360, "подходит": True}]), "нет причины у номера 1145360"),
], ids=["пропущен", "чужой", "дважды", "научная-строка", "научная-число", "подходит-не-bool",
        "текст", "объект", "оборван", "пустая-причина", "без-причины"])
def test_пачка_негодный_ответ(среда, итог, причина):
    модель = ПоддельнаяМодель([*ВСЕ_ВЫЗОВЫ, итог])
    with pytest.raises(ValueError, match=причина):
        судить(среда, модель)


def test_пачка_нет_вызова_по_номеру(среда):
    """Модель ответила по всем номерам, а сведения о Hades не брала — ответ не принят."""
    модель = ПоддельнаяМодель([*ВСЕ_ВЫЗОВЫ[:2], _итог((367520, True), (70010000003208, False), (1145360, True))])
    with pytest.raises(ValueError, match="1145360.*steam_game"):
        судить(среда, модель)


def test_пачка_вызов_не_тем_инструментом_и_с_ошибкой(среда):
    """Игру eShop спросили у steam_game — вызова нужного инструмента не было; сбой инструмента
    сведений не дал — тоже не вызов."""
    итог = _итог((367520, True), (70010000003208, False), (1145360, True))
    модель = ПоддельнаяМодель([[вызов(367520)], [вызов(70010000003208)], [вызов(1145360)], итог])
    with pytest.raises(ValueError, match="70010000003208.*eshop_game"):
        судить(среда, модель)
    модель = ПоддельнаяМодель([*ВСЕ_ВЫЗОВЫ, итог])
    with pytest.raises(ValueError, match="367520.*steam_game"):
        судить(среда, модель, ПоддельныеСоединения(сбои={367520}))


def test_пачка_длинный_ответ_обрезан(среда):
    модель = ПоддельнаяМодель([*ВСЕ_ВЫЗОВЫ, "не json " * 200])
    with pytest.raises(ValueError) as сбой:
        судить(среда, модель)
    assert len(str(сбой.value)) < 500


def test_пачка_инструмент_не_поднялся(среда):
    """Сервер MCP не отдал steam_game — суд не начинается, причина названа."""

    class Пустые(ПоддельныеСоединения):
        async def набор(self, каталог):
            return Инструменты([], None, жалоба="MCP steam: сервер не ответил за 30 с")

    модель = ПоддельнаяМодель([])
    with pytest.raises(RuntimeError, match="сервер не ответил"):
        судить(среда, модель, Пустые())
    assert модель.запросы == []


def test_пачка_инварианты_в_запросе_без_запретов_игр(среда):
    """Ограничения человека идут блоком «Инварианты», а «Не предлагать: …» программа уже
    отсеяла — в запрос они не идут."""
    import taste
    from myharness import project_card
    from steam_store import Игра

    taste.запретить(среда, Игра(1086940, "Baldur's Gate 3"))
    карточка, _ = project_card.load(среда)
    project_card.add_line(карточка, project_card.РАЗДЕЛ_ОГРАНИЧЕНИЙ, "без хорроров")
    модель = ПоддельнаяМодель([*ВСЕ_ВЫЗОВЫ, _итог((367520, True), (70010000003208, False), (1145360, True))])
    судить(среда, модель)
    запрос = модель.запросы[0][-1]["content"]
    assert "Инварианты" in запрос and "без хорроров" in запрос
    assert "Не предлагать" not in запрос and "1086940" not in запрос


def test_пачка_чужой_инструмент_отвергнут(среда):
    """Модель позвала `steam_search`, которого в наборе суда нет: вызов не исполнен, модель
    получила ошибку инструмента."""
    соединения = ПоддельныеСоединения()
    поиск = {"id": "s1", "type": "function",
             "function": {"name": "mcp__steam__steam_search", "arguments": json.dumps({"tag": 122})}}
    модель = ПоддельнаяМодель([[поиск], *ВСЕ_ВЫЗОВЫ, _итог((367520, True), (70010000003208, False), (1145360, True))])
    судить(среда, модель, соединения)
    assert all(имя != "mcp__steam__steam_search" for имя, _ in соединения.вызовы)
    ответ = next(с for с in модель.запросы[1] if с.get("role") == "tool")
    assert ответ["content"].startswith("ошибка инструмента") and "не разрешён" in ответ["content"]


def test_пачка_срок(среда, monkeypatch):
    """Модель молчит дольше срока пачки — `ValueError`, задание уведёт пачку на запасной путь."""

    class Молчит(ПоддельнаяМодель):
        async def stream_chat(self, model, messages, params=None, tools=None):
            await asyncio.sleep(3600)
            yield api.StreamEvent("content", "")

    monkeypatch.setattr(judge, "СРОК_ПАЧКИ_С", 0.05)
    with pytest.raises(ValueError, match="не уложилась в 0.05 с"):
        судить(среда, Молчит([]))
    assert judge.СРОК_ПАЧКИ_С == 0.05 and "СРОК_ПАЧКИ_С = 300.0" in Path(judge.__file__).read_text("utf-8")


def test_пачка_каталог_пишется_только_при_изменении(среда):
    свой = judge.подготовить_каталог_инструментов(среда)
    файлы = [свой / ".mcp.json", свой / ".claude" / "settings.json"]
    import os

    for ф in файлы:
        os.utime(ф, (1, 1))
    judge.подготовить_каталог_инструментов(среда)
    assert all(ф.stat().st_mtime == 1 for ф in файлы)
    (свой / ".mcp.json").write_text("{}", "utf-8")
    judge.подготовить_каталог_инструментов(среда)
    assert json.loads((свой / ".mcp.json").read_text("utf-8"))["mcpServers"]["steam"]
    assert not list(свой.rglob("*.tmp")) and not list(свой.rglob(".*.tmp"))


def test_профиль_пачки():
    текст = (ПРОФИЛИ / "отбор-игр-mcp.md").read_text("utf-8")
    assert "не повторяй вызов" in текст and "сведений нет" in текст
    assert "Не отвечай, пока" not in текст
    assert "Данные магазина" in текст
