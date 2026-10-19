"""Оценка игр eShop без пары в Steam: оценка игроков Metacritic.

Открытого API у Metacritic нет. Модуль ходит туда же, куда сам сайт: в его заднюю часть
`backend.metacritic.com` — поиск игры по названию даёт слаг и платформы, сводка оценок игроков
по слагу и платформе даёт число всех и положительных отзывов. Сайт сменит устройство — разбор
вернёт пусто или `ОшибкаМагазина`.

Берётся оценка игроков, а не критиков. Сводка критиков устроена так же, но доля положительных
у критиков почти всегда около 100 % и игры не различает (замер 2026-10-19, Donkey Kong
Bananza: 143 из 143). У игроков — тысячи отзывов и живая доля (2848 из 3293): её и ждёт
формула SteamDB, как отзывы Steam.

Сводка переводится на русский без слова «Metacritic»: источник следует из магазина плашки —
игра без пары в Steam всегда оценена здесь. Незнакомая сводка — пустая, в отличие от
`steam_store`, где остаётся исходный английский текст: сознательно, чтобы английское слово не
попало на страницу; доля и число сохраняются.

Состояния у модуля нет, на диск он не пишет.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx2

import steam_store as ss
from eshop_store import _json, _запрос, сравнимое, чистое_имя
from steam_store import НЕТ_ОТЗЫВОВ, Игра, Отзывы

АДРЕС = "https://backend.metacritic.com"
СВОДКИ = {"Universal acclaim": "всеобщее признание", "Generally favorable": "в целом хорошие",
          "Mixed or average": "смешанные", "Generally unfavorable": "в целом плохие",
          "Overwhelming dislike": "всеобщая неприязнь"}
SWITCH = "Nintendo Switch"
SWITCH_2 = "Nintendo Switch 2"


def разобрать_поиск(данные: object, название: str) -> tuple[str, tuple[str, ...]] | None:
    """Слаг и платформы записи, чьё имя точно совпало с названием; `None` — совпадения нет.
    Берётся своя запись, а не первая: дополнение («…: DK Island & Emerald Rush») стоит в выдаче
    рядом с игрой, а ложная оценка хуже никакой."""
    записи = данные.get("data") if isinstance(данные, dict) else None
    записи = записи.get("items") if isinstance(записи, dict) else None
    if not isinstance(записи, list):
        raise ss.ОшибкаМагазина("ответ поиска Metacritic без items")
    ключ = сравнимое(название)
    if not ключ:
        # Имя без латиницы и цифр: пустой ключ совпал бы с любой такой же записью.
        return None
    for з in записи:
        if not isinstance(з, dict) or not isinstance(з.get("slug"), str) or not з["slug"]:
            continue
        if сравнимое(str(з.get("title") or "")) != ключ:
            continue
        платформы = tuple(
            str(п["name"]) for п in з.get("platforms") or [] if isinstance(п, dict) and п.get("name")
        )
        return з["slug"], платформы
    return None


def _слаг(имя: str) -> str:
    # У платформ в выдаче `slug` пуст; сайт принимает имя в нижнем регистре через дефисы.
    return "-".join(имя.lower().split())


def платформа(платформы: tuple[str, ...], switch2: bool) -> str | None:
    """Слаг платформы, по которой брать оценку: сначала платформа игры, затем другая платформа
    Switch. Других платформ Switch нет — `None`: оценка с ПК или Wii U не про эту игру."""
    for имя in (SWITCH_2, SWITCH) if switch2 else (SWITCH, SWITCH_2):
        if имя in платформы:
            return _слаг(имя)
    return None


def разобрать_оценку(данные: object) -> Отзывы:
    """Оценка игроков: сводка по-русски, положительных и всего. Отзывов нет — `НЕТ_ОТЗЫВОВ`;
    незнакомая сводка — без сводки, английское слово на русскую страницу не идёт."""
    запись = данные.get("data") if isinstance(данные, dict) else None
    запись = запись.get("item") if isinstance(запись, dict) else None
    if not isinstance(запись, dict):
        raise ss.ОшибкаМагазина("ответ оценки Metacritic без item")
    всего, положительных = запись.get("reviewCount"), запись.get("positiveCount")
    if not isinstance(всего, int) or всего <= 0 or not isinstance(положительных, int):
        return НЕТ_ОТЗЫВОВ
    return Отзывы(СВОДКИ.get(str(запись.get("sentiment") or ""), ""), положительных, всего)


def клиент() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=ss.ОЖИДАНИЕ, headers={"User-Agent": ss.ПРЕДСТАВЛЕНИЕ})


async def отзывы(к: httpx2.AsyncClient, игра: Игра) -> Отзывы:
    """Оценка игроков игры eShop по её платформе Switch. Записи с тем же именем нет, у неё нет
    платформы Switch либо страницы оценки — `НЕТ_ОТЗЫВОВ`, второго запроса не делается."""
    имя = чистое_имя(игра.имя)
    # Строка поиска — кусок пути: «&», «/» и «?» в имени экранируются, иначе адрес порвётся.
    # Без `limit`: умолчание сайта — 5 записей (замер 2026-10-19: «mario» — 5 из 197), своего
    # числа не держим; точное имя стоит в выдаче первым («super mario odyssey»).
    ответ = await _запрос(к, "GET", f"{АДРЕС}/finder/metacritic/search/{quote(имя, safe='')}/web",
                          доводы={"mcoTypeId": "13"})
    # 404 у поиска — не «игры нет», а сменившийся адрес: `_json` даст `ОшибкаМагазина`.
    найдена = разобрать_поиск(_json(ответ), имя)
    if найдена is None:
        return НЕТ_ОТЗЫВОВ
    слаг, платформы = найдена
    слаг_платформы = платформа(платформы, switch2=SWITCH_2 in игра.платформы)
    if слаг_платформы is None:
        return НЕТ_ОТЗЫВОВ
    ответ = await _запрос(к, "GET", f"{АДРЕС}/reviews/metacritic/user/games/{слаг}/stats/web",
                          доводы={"platform": слаг_платформы})
    # Замер 2026-10-19: 404 бывает только у несуществующего слага; чужую платформу сайт молча
    # подменяет своей (200). Слаг взят из поиска, так что 404 — «отзывов нет», а не сбой.
    if ответ.status_code == 404:
        return НЕТ_ОТЗЫВОВ
    return разобрать_оценку(_json(ответ))
