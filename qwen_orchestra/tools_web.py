"""Веб-инструменты для локального агента (поиск и чтение страниц)."""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse


_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset=[\"']?\s*([\w-]+)", re.IGNORECASE)
_MAIN_RE = re.compile(r"<(main|article)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_MIN_MAIN_CHARS = 300


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS and not self._skip:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS and not self._skip:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if text:
            self._chunks.append(text + " ")

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = (" ".join(line.split()) for line in raw.split("\n"))
        return "\n".join(line for line in lines if line)


def _extract_text(html: str) -> str:
    """Текст страницы; если есть <main>/<article> достаточного размера — берём его."""
    candidates = [html]
    main_blocks = [m.group(0) for m in _MAIN_RE.finditer(html)]
    if main_blocks:
        candidates.insert(0, max(main_blocks, key=len))
    for chunk in candidates:
        parser = _TextExtractor()
        try:
            parser.feed(chunk)
        except Exception:  # noqa: BLE001
            text = re.sub(r"<[^>]+>", " ", chunk)
        else:
            text = parser.text()
        if len(text) >= _MIN_MAIN_CHARS or chunk is candidates[-1]:
            return text
    return ""


def _detect_charset(header_charset: str | None, raw: bytes) -> str:
    if header_charset:
        return header_charset
    m = _META_CHARSET_RE.search(raw[:4096])
    if m:
        return m.group(1).decode("ascii", errors="ignore") or "utf-8"
    return "utf-8"


_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
    "kubernetes.default",
    "kubernetes.default.svc",
}


def _is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _host_blocked(host: str) -> str | None:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return "Ошибка: пустой хост в URL"
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return "Ошибка: запрещён доступ к локальным адресам"
    # IP в URL без DNS
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_blocked_ip(host):
            return "Ошибка: запрещён доступ к private/loopback IP"
        return None
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return f"Ошибка: не удалось разрешить хост ({exc})"
    for info in infos:
        ip = info[4][0]
        if _is_blocked_ip(ip):
            return f"Ошибка: хост резолвится в запрещённый адрес ({ip})"
    return None


def _url_blocked(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "Ошибка: URL должен начинаться с http:// или https://"
    if parsed.username or parsed.password:
        return "Ошибка: URL с credentials запрещён"
    return _host_blocked(parsed.hostname or "")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Проверяем цель каждого редиректа (SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        err = _url_blocked(newurl)
        if err:
            raise urllib.error.HTTPError(newurl, 403, err, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SEARCH_TIMEOUT = 10
_TIMELIMITS = {"d", "w", "m", "y"}
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_NEWS_RE = re.compile(r"новост|news|заголовк|headline|происшеств", re.IGNORECASE)
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")


def _pick_region(query: str, region: str | None) -> str:
    """Явный регион модели, иначе по алфавиту запроса (ru-ru / us-en)."""
    reg = (region or "").strip().lower()
    if _REGION_RE.match(reg) or reg == "wt-wt":
        return reg
    return "ru-ru" if _CYRILLIC_RE.search(query) else "us-en"


def _ddgs_call(query: str, *, news: bool, region: str, timelimit: str | None, limit: int):
    from ddgs import DDGS

    client = DDGS(timeout=SEARCH_TIMEOUT)
    kwargs = {"region": region, "max_results": limit}
    if timelimit:
        kwargs["timelimit"] = timelimit
    return list(client.news(query, **kwargs) if news else client.text(query, **kwargs))


def web_search(
    query: str,
    max_results: int = 5,
    *,
    region: str | None = None,
    timelimit: str | None = None,
    news: bool | None = None,
) -> str:
    """Поиск в интернете через DuckDuckGo.

    `region` — `ru-ru` / `us-en` / … (по умолчанию по алфавиту запроса);
    `timelimit` — `d` день / `w` неделя / `m` месяц / `y` год;
    `news` — лента новостей (по умолчанию включается на «новости»/«news»).
    """
    query = (query or "").strip()
    if not query:
        return "Ошибка: пустой поисковый запрос."

    try:
        import ddgs  # noqa: F401
    except ImportError:
        return "Ошибка: пакет ddgs не установлен. Выполните: pip install ddgs"

    tl = (timelimit or "").strip().lower() or None
    if tl not in _TIMELIMITS:
        tl = None
    use_news = bool(_NEWS_RE.search(query)) if news is None else bool(news)
    reg = _pick_region(query, region)
    limit = max(1, min(int(max_results or 5), 10))

    results: list[dict] = []
    error: Exception | None = None
    modes = [True, False] if use_news else [False]
    for mode in modes:
        try:
            results = _ddgs_call(query, news=mode, region=reg, timelimit=tl, limit=limit)
        except Exception as exc:  # noqa: BLE001
            error = exc
            continue
        if results:
            use_news = mode
            break
    if not results:
        if error is not None:
            return f"Ошибка поиска: {error}"
        return f"По запросу «{query}» ничего не найдено."

    kind = "Новости" if use_news else "Результаты поиска"
    scope = f" [{reg}" + (f", за {tl}" if tl else "") + "]"
    lines = [f"{kind} по «{query}»{scope}:"]
    for i, item in enumerate(results, 1):
        title = item.get("title") or "(без названия)"
        href = item.get("href") or item.get("url") or ""
        body = item.get("body") or ""
        meta = " · ".join(
            str(x) for x in (item.get("source"), item.get("date")) if x
        )
        lines.append(
            f"{i}. {title}" + (f" ({meta})" if meta else "") + f"\n   URL: {href}\n   {body}"
        )
    return "\n".join(lines)


def fetch_url(url: str, max_chars: int = 4000) -> str:
    """Скачать страницу и извлечь текст (без private/loopback — защита от SSRF)."""
    url = (url or "").strip()
    blocked = _url_blocked(url)
    if blocked:
        return blocked

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        },
        method="GET",
    )
    # Читаем ограниченный объём байт (HTML раздут относительно текста)
    max_bytes = max(max_chars * 8, 64_000)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(req, timeout=20) as resp:
            final_url = resp.geturl() or url
            again = _url_blocked(final_url)
            if again:
                return again
            ctype = (resp.headers.get_content_type() or "").lower()
            if ctype and not any(
                x in ctype for x in ("text/", "html", "xml", "json", "javascript")
            ):
                return f"Пропуск {url}: нетекстовый Content-Type ({ctype})"
            header_charset = resp.headers.get_content_charset()
            raw = resp.read(max_bytes)
            charset = _detect_charset(header_charset, raw)
    except urllib.error.HTTPError as exc:
        return f"HTTP ошибка {exc.code} при загрузке {url}"
    except Exception as exc:  # noqa: BLE001
        return f"Не удалось загрузить {url}: {exc}"

    try:
        html = raw.decode(charset, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")

    text = _extract_text(html).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[обрезано]"
    if not text:
        return f"Страница {url} загружена, но текст извлечь не удалось."
    return f"Содержимое {url}:\n{text}"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Искать актуальную информацию в интернете. "
                "Используй для новостей, погоды, цен, фактов после даты обучения, "
                "проверки свежих данных."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Короткий поисковый запрос на русском или английском",
                    },
                    "timelimit": {
                        "type": "string",
                        "enum": ["d", "w", "m", "y"],
                        "description": "Свежесть: d — сутки, w — неделя, m — месяц, y — год. Для новостей/цен/курсов.",
                    },
                    "news": {
                        "type": "boolean",
                        "description": "true — искать в новостных лентах (события, заголовки)",
                    },
                    "region": {
                        "type": "string",
                        "description": "Регион, например ru-ru, us-en, de-de (по умолчанию по языку запроса)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Открыть конкретный URL и прочитать текст страницы. "
                "Используй после поиска, когда нужен полный текст источника."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Полный URL страницы (https://...)",
                    }
                },
                "required": ["url"],
            },
        },
    },
]

TOOL_IMPL = {
    "web_search": lambda args: web_search(
        args.get("query", ""),
        region=args.get("region"),
        timelimit=args.get("timelimit"),
        news=args.get("news"),
    ),
    "fetch_url": lambda args: fetch_url(args.get("url", "")),
}
