"""Магазин Nintendo eShop, зона «Европа»: каталог и полное описание.

Открытого API у eShop нет. Модуль ходит туда же, куда сайт nintendo.com в Европе: каталог — поиск
Solr `searching.nintendo-europe.com/<язык>/select`, полное описание — вкладка обзора страницы игры
(HTML). Цены со скидками — общий `eshop_store.цены` (`api.ec` с `country` страны). Сайт сменит
устройство — разбор вернёт пусто или `ОшибкаМагазина`.

Каталог у зоны один: тот же `fs_id` и тот же nsuid на всех языках, язык (`регион.каталог`) меняет
имена жанров, краткое описание и адрес страницы (`/de-de/Spiele/…`). Глубина выдачи не ограничена
(замер: `start=20000` отвечает), предела вроде 1000 записей Algolia у Америк нет.

Номера у записи — список `nsuid_txt` (замер 2026-10-19 на 1000 играх по популярности): у 731 —
один номер игры `7001…`; рядом бывает набор `7007…` (игра с дополнениями, «Bundle»), пакет
обновления до издания Switch 2 `7005…` либо второй `7001…` — бесплатный пропуск друга (цена 0
в `api.ec`). Платформа у записи одна (`playable_on_txt`: HAC — Switch, BEE — Switch 2), у каждой
платформы своя запись, номера между платформами не делятся. У 52 записей номера только `7007…` —
сборники («Super Mario Galaxy + Super Mario Galaxy 2»): продаются они, номер берётся их. У 35
записей номеров нет вовсе (анонсы без предзаказа) — купить нечего, такие отброшены. Демоверсий и
дополнений отдельными записями `type:GAME` нет: у Switch все записи этого типа (фасет `type`).

Состояния у модуля нет, на диск он не пишет.
"""

from __future__ import annotations

import dataclasses
import html
import re
from datetime import UTC, date, datetime

import httpx2

import eshop_store as es
import steam_store as ss
from eshop_store import Регион
from steam_store import Игра

ПОИСК = "https://searching.nintendo-europe.com"
ДЛИНА_СТРАНИЦЫ = es.ДЛИНА_СТРАНИЦЫ
# Только игры Switch и Switch 2: в каталоге и 3DS, и Wii U, и железо.
_ИГРЫ = ("type:GAME", "playable_on_txt:(HAC OR BEE)")
ПЛАТФОРМЫ = {"HAC": "Nintendo Switch", "BEE": "Nintendo Switch 2"}  # имена как у Америк
# Поля ответа: без `fl` запись несёт ~70 полей, разбору нужны эти.
_ПОЛЯ = ",".join((
    "title", "nsuid_txt", "fs_id", "type", "playable_on_txt", "date_from", "pretty_date_s",
    "pretty_game_categories_txt", "excerpt", "product_catalog_description_s", "url",
    "wishlist_email_banner460w_image_url_s", "image_url_h2x1_s", "image_url", "image_url_sq_s",
))
# Обложки по порядку: широкая 460 — как у Steam и Америк (есть у 19 911 из 19 912 игр Switch),
# затем 2:1, затем `image_url` (у старых — квадрат, у части — коробка), затем квадрат.
_ОБЛОЖКИ = ("wishlist_email_banner460w_image_url_s", "image_url_h2x1_s", "image_url", "image_url_sq_s")
# `date_from` 2050-12-31 — дата не объявлена («TBD», «n. n. b.», «Date à déterminer»…).
_ЗАГЛУШКА_ДАТЫ = "2050-12-31"
НЕ_ОБЪЯВЛЕНА = "дата не объявлена"
# Точный день в `pretty_date_s`: «22/10/2026», «22.10.2026», «22-10-2026» — по языку. Иначе
# («2027») `date_from` — заглушка 31 декабря, дня нет.
_ТОЧНЫЙ_ДЕНЬ = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}")
_ИГРА = "7001"
# Знаки языка запросов Solr: название ищется как текст, не как запрос.
_ЗНАКИ_SOLR = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

# Вкладка обзора страницы игры: `id` переводится («Overview», «_bersicht»), `data-section-type` — нет.
_ОБЗОР = re.compile(r'<section\b[^>]*\bdata-section-type="overview"[^>]*>(.*?)</section>', re.DOTALL | re.IGNORECASE)
# `noscript` во вкладке — разметка видео для поисковиков («Video: <имя>» и подпись ролика).
_КОД = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>|<!--.*?-->", re.DOTALL | re.IGNORECASE)
# Приглашение подписаться под встроенным роликом: «Subscribe to <канал> on» и кнопка «YouTube»
# (по-немецки «Abonniere … auf») — два блока `youtube-sub-…`, у второго внутри ещё `div`.
_ПОДПИСКА = re.compile(r'<div\b[^>]*\bclass="[^"]*youtube-sub[^"]*"[^>]*>.*?</div>', re.DOTALL | re.IGNORECASE)
# Границы блоков: по ним текст делится на строки для отбора строк видео.
_БЛОК = re.compile(r"</?(?:p|div|h[1-6]|li|ul|ol|br|hr|section|table|tr|td|th)\b[^>]*>", re.IGNORECASE)
# После текста игры во вкладке — оговорки магазина о покупке и предзаказе (`gamedisclaimer-box`).
_ОГОВОРКИ = re.compile(r'<[^>]*\bclass="[^"]*disclaimer', re.IGNORECASE)
_РАЗМЕТКА = re.compile(r"<[^>]+>")


def _сейчас() -> datetime:
    return datetime.now(UTC)


def номер(номера: object) -> int | None:
    """nsuid игры из `nsuid_txt`: первый по порядку `7001…`, а нет его — первый номер (набор
    `7007…`). `None` — номеров нет. Решает порядок, не число: у SPLIT FICTION номер бесплатного
    пропуска друга меньше номера игры, но стоит вторым.

    У записи только с наборами `7007…` берётся первый, а он не всегда дешёвый: у 12 из 19 912 игр
    первый набор дороже (Call of Duty: Modern Warfare 4 — 99,99 против 69,99), цена такой игры
    может быть завышена. Как выбрать дешёвый без лишнего запроса цен, поиск не говорит."""
    if not isinstance(номера, list):
        return None
    годные = [н for н in номера if isinstance(н, str) and н.isdigit()]
    for н in годные:
        if н.startswith(_ИГРА):
            return int(н)
    return int(годные[0]) if годные else None


def _дата(з: dict) -> tuple[date | None, str]:
    начало = з.get("date_from")
    текст = str(з.get("pretty_date_s") or "")
    if not isinstance(начало, str) or not начало:
        return None, текст
    if начало.startswith(_ЗАГЛУШКА_ДАТЫ):
        return None, НЕ_ОБЪЯВЛЕНА
    if _ТОЧНЫЙ_ДЕНЬ.fullmatch(текст):
        try:
            день = date.fromisoformat(начало[:10])
        except ValueError:
            return None, текст
        return день, день.isoformat()
    return None, текст


def _вышла(з: dict, сейчас: datetime) -> bool:
    """Вышла, если `date_from` прошла; без разборной даты — вышла: будущие все с датой."""
    try:
        начало = datetime.fromisoformat(str(з.get("date_from")).replace("Z", "+00:00"))
    except ValueError:
        return True
    if начало.tzinfo is None:
        начало = начало.replace(tzinfo=UTC)
    return начало <= сейчас


def разобрать_запись(данные: object, сейчас: datetime) -> Игра | None:
    """Игра из записи поиска. `None` — не игра Switch или Switch 2 либо запись без nsuid: её не
    купить. Цены в записи нет: она — из `api.ec` (`eshop_store.цены`)."""
    з = данные
    if not isinstance(з, dict) or з.get("type") != "GAME":
        return None
    платформы = tuple(ПЛАТФОРМЫ[п] for п in з.get("playable_on_txt") or [] if п in ПЛАТФОРМЫ)
    nsuid = номер(з.get("nsuid_txt"))
    if not платформы or nsuid is None:
        return None
    дата, дата_текст = _дата(з)
    краткое = з.get("excerpt") or з.get("product_catalog_description_s") or ""
    обложка = next((з[п] for п in _ОБЛОЖКИ if isinstance(з.get(п), str) and з[п]), "")
    адрес = з.get("url")
    return Игра(
        номер=nsuid,
        имя=" ".join(str(з.get("title") or nsuid).split()),
        описание=ss._без_разметки(str(краткое)),
        жанры=tuple(str(ж) for ж in з.get("pretty_game_categories_txt") or []),
        дата_текст=дата_текст,
        дата=дата,
        вышла=_вышла(з, сейчас),
        обложка=обложка,
        адрес=es.САЙТ + адрес if isinstance(адрес, str) and адрес.startswith("/") else "",
        объект=str(з.get("fs_id") or ""),
        платформы=платформы,
    )


def разобрать_выдачу(данные: object, сейчас: datetime) -> list[Игра]:
    """Игры страницы поиска в порядке выдачи, без записей, которые не купить."""
    ответ = данные.get("response") if isinstance(данные, dict) else None
    if not isinstance(ответ, dict) or not isinstance(ответ.get("docs"), list):
        raise ss.ОшибкаМагазина("ответ поиска eShop Европы без response.docs")
    return [и for з in ответ["docs"] if (и := разобрать_запись(з, сейчас)) is not None]


def разобрать_описание(страница: str) -> str:
    """Полное описание — текст вкладки обзора страницы игры без оговорок магазина; не нашлось — "".
    В поиске описание в одну фразу (медиана 90 знаков), суду местной модели его мало."""
    найдено = _ОБЗОР.search(страница)
    if not найдено:
        return ""
    # Сначала код: в скриптах вкладки встречается слово `disclaimer`.
    текст = _КОД.sub(" ", найдено.group(1))
    оговорки = _ОГОВОРКИ.search(текст)
    if оговорки:
        текст = текст[:оговорки.start()]
    # Граница блока — знак `\0`, не перевод строки: переводы строк бывают и внутри тегов.
    текст = _БЛОК.sub("\0", _ПОДПИСКА.sub(" ", текст))
    строки = (" ".join(html.unescape(_РАЗМЕТКА.sub(" ", с)).split()) for с in текст.split("\0"))
    # Строки встроенного видео, если разметка ролика сменится: с «YouTube» и «Video: <имя>».
    return " ".join(с for с in строки if с and "youtube" not in с.casefold() and not с.startswith("Video:"))


def _адрес_поиска(регион: Регион) -> str:
    return f"{ПОИСК}/{регион.каталог}/select"


async def _поиск(к: httpx2.AsyncClient, регион: Регион, доводы: dict[str, object]) -> list[Игра]:
    ответ = await es._запрос(к, "GET", _адрес_поиска(регион), доводы={"wt": "json", "fl": _ПОЛЯ, **доводы})
    return разобрать_выдачу(es._json(ответ), _сейчас())


async def витрина(к: httpx2.AsyncClient, регион: Регион, *, будущие: bool, страница: int) -> list[Игра]:
    """Одна страница каталога — до 50 игр. Пустой список — выдача кончилась. Будущие — по дате
    выхода (`date_from` впереди, «дата не объявлена» в конце); вышедшие — по рангу загрузок."""
    отбор = ("date_from:[NOW TO *]", "date_from asc") if будущие else ("downloads_rank_i:[1 TO *]", "downloads_rank_i asc")
    return await _поиск(к, регион, {
        "q": "*", "fq": [*_ИГРЫ, отбор[0]], "sort": отбор[1],
        "rows": str(ДЛИНА_СТРАНИЦЫ), "start": str(ДЛИНА_СТРАНИЦЫ * страница),
    })


async def найти_по_названию(к: httpx2.AsyncClient, регион: Регион, название: str) -> list[Игра]:
    """Поиск каталога по названию, до 10 записей в порядке релевантности Solr."""
    if not название.strip():
        return []
    return await _поиск(к, регион, {"q": _ЗНАКИ_SOLR.sub(r"\\\1", название), "fq": list(_ИГРЫ), "rows": "10"})


async def запись(к: httpx2.AsyncClient, регион: Регион, объект: str) -> Игра | None:
    """Запись каталога по `fs_id`. `None` — кода нет в каталоге либо запись не игра Switch."""
    if not объект.isdigit():
        # Код идёт в фильтр Solr: не число — не код каталога.
        return None
    игры = await _поиск(к, регион, {"q": "*", "fq": ["type:GAME", f"fs_id:{объект}"], "rows": "1"})
    return игры[0] if игры else None


async def описание(к: httpx2.AsyncClient, регион: Регион, адрес: str) -> str:
    """Полное описание со страницы игры; страницы нет — "". Язык уже в адресе из поиска
    (`/de-de/…`); `регион` — общий договор зон."""
    ответ = await es._запрос(к, "GET", адрес)
    return "" if ответ.status_code == 404 else разобрать_описание(ответ.text)


async def игра(к: httpx2.AsyncClient, регион: Регион, объект: str) -> Игра | None:
    """Запись каталога с полным описанием страницы игры вместо краткого."""
    найдена = await запись(к, регион, объект)
    if найдена is None or not найдена.адрес:
        return найдена
    полное = await описание(к, регион, найдена.адрес)
    return dataclasses.replace(найдена, описание=полное) if полное else найдена


async def найти_в_eshop(к: httpx2.AsyncClient, регион: Регион, игра: Игра) -> Игра | None:
    """Та же игра в eShop Европы — первое точное совпадение имён в выдаче поиска. У Switch и
    Switch 2 записи разные, имена совпадают — первая в выдаче та, что Solr ставит выше."""
    ключ = es.сравнимое(игра.имя)
    if not ключ:
        return None
    for найдена in await найти_по_названию(к, регион, es.чистое_имя(игра.имя)):
        if es.сравнимое(найдена.имя) == ключ:
            return найдена
    return None
