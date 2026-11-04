"""Fetch and parse Royal Road fiction + chapter pages."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://www.royalroad.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_FICTION_RE = re.compile(r"royalroad\.com/fiction/(\d+)", re.I)
_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr",
               "blockquote", "table", "ul", "ol", "hr", "pre"}
# Royal Road injects hidden "this story was stolen" paragraphs into chapter text.
# They are hidden via CSS, but we also filter by wording as a fallback.
_THEFT_RE = re.compile(
    r"(royal\s*road|royalroad).{0,120}(stolen|unlawful|without (the )?(author'?s )?"
    r"(consent|permission)|report|amazon|misappropriat|pilfered|taken)|"
    r"(stolen|unlawful|misappropriat|pilfered|taken).{0,120}(royal\s*road|royalroad)",
    re.I,
)


class RoyalRoadError(Exception):
    pass


@dataclass
class Chapter:
    id: int
    title: str
    url: str
    date: str | None = None


@dataclass
class Fiction:
    id: int
    title: str
    author: str
    cover: str | None
    url: str
    chapters: list[Chapter] = field(default_factory=list)


@dataclass
class ChapterText:
    id: int
    title: str
    paragraphs: list[str]


def parse_fiction_id(value: str) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    m = _FICTION_RE.search(value)
    if not m:
        raise RoyalRoadError("That doesn't look like a Royal Road fiction URL.")
    return int(m.group(1))


async def fetch_html(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
    except httpx.HTTPError as e:
        raise RoyalRoadError(f"Could not reach Royal Road: {e}") from e
    if r.status_code == 404:
        raise RoyalRoadError("Royal Road returned 404 (not found).")
    if r.status_code >= 400:
        raise RoyalRoadError(f"Royal Road returned HTTP {r.status_code}.")
    return r.text


def _abs(url: str) -> str:
    return url if url.startswith("http") else BASE + url


def parse_fiction_page(html: str, fiction_id: int) -> Fiction:
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop: str) -> str | None:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return tag.get("content") if tag else None

    h1 = soup.select_one(".fic-title h1") or soup.select_one("h1")
    title = h1.get_text(strip=True) if h1 else (meta("og:title") or f"Fiction {fiction_id}")
    author_tag = soup.select_one(".fic-title h4 a") or soup.select_one(".fic-title a[href*='/profile/']")
    author = author_tag.get_text(strip=True) if author_tag else (meta("books:author") or "")
    cover_tag = soup.select_one(".cover-art-container img") or soup.select_one("img.thumbnail")
    cover = (cover_tag.get("src") if cover_tag else None) or meta("og:image")
    if cover and cover.startswith("/"):
        cover = BASE + cover

    chapters: list[Chapter] = []
    # Preferred: the page embeds the full chapter list as JSON.
    m = re.search(r"window\.chapters\s*=\s*(\[.*?\])\s*;", html, re.S)
    if m:
        try:
            for c in json.loads(m.group(1)):
                if c.get("visible", 1) in (0, False):
                    continue
                chapters.append(Chapter(id=int(c["id"]), title=c.get("title", "").strip(),
                                        url=_abs(c["url"]), date=c.get("date")))
        except (ValueError, KeyError, TypeError):
            chapters = []
    # Fallback: the chapters table.
    if not chapters:
        for row in soup.select("#chapters tr.chapter-row, table#chapters tbody tr"):
            link = row.select_one("a[href*='/chapter/']")
            href = row.get("data-url") or (link.get("href") if link else None)
            if not href:
                continue
            cm = re.search(r"/chapter/(\d+)", href)
            if not cm:
                continue
            t = row.find("time")
            chapters.append(Chapter(id=int(cm.group(1)),
                                    title=(link.get_text(strip=True) if link else f"Chapter {cm.group(1)}"),
                                    url=_abs(href), date=t.get("datetime") if t else None))

    return Fiction(id=fiction_id, title=title, author=author, cover=cover,
                   url=f"{BASE}/fiction/{fiction_id}", chapters=chapters)


def _hidden_classes(soup: BeautifulSoup) -> set[str]:
    classes: set[str] = set()
    for style in soup.find_all("style"):
        css = style.get_text()
        for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
            selectors, body = m.group(1), m.group(2).replace(" ", "").lower()
            if "display:none" in body or "visibility:hidden" in body or "speak:never" in body:
                classes.update(re.findall(r"\.([A-Za-z_][\w-]*)", selectors))
    return classes


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_chapter_page(html: str, chapter_id: int) -> ChapterText:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("div.chapter-inner.chapter-content") or soup.select_one("div.chapter-content")
    if content is None:
        raise RoyalRoadError("Couldn't find chapter text on the page (layout may have changed).")

    title_tag = soup.select_one(".fic-header h1") or soup.select_one("h1")
    title = title_tag.get_text(strip=True) if title_tag else f"Chapter {chapter_id}"

    hidden = _hidden_classes(soup)
    for el in list(content.find_all(True)):
        if el.decomposed:
            continue
        cls = set(el.get("class") or [])
        style = (el.get("style") or "").replace(" ", "").lower()
        if (cls & hidden) or "display:none" in style or el.name in ("script", "style", "noscript"):
            el.decompose()

    # Turn structure into line breaks so paragraphs survive get_text().
    for br in content.find_all("br"):
        br.replace_with("\n")
    # Table cells (LitRPG status screens) get read as short sentences.
    for cell in content.find_all(["td", "th"]):
        text = _clean(cell.get_text(" "))
        if text and text[-1] not in ".!?:;,":
            text += "."
        cell.clear()
        cell.append(NavigableString(text + " "))
    for el in content.find_all(_BLOCK_TAGS):
        el.insert_before(NavigableString("\n"))
        el.insert_after(NavigableString("\n"))

    paragraphs = []
    for line in content.get_text().split("\n"):
        line = _clean(line)
        if not line or not re.search(r"\w", line):
            continue
        if len(line) < 300 and _THEFT_RE.search(line):
            continue
        paragraphs.append(line)

    if not paragraphs:
        raise RoyalRoadError("Chapter text was empty.")
    return ChapterText(id=chapter_id, title=title, paragraphs=paragraphs)


async def get_fiction(client: httpx.AsyncClient, fiction_id: int) -> Fiction:
    html = await fetch_html(client, f"{BASE}/fiction/{fiction_id}")
    return parse_fiction_page(html, fiction_id)


async def get_chapter(client: httpx.AsyncClient, chapter: Chapter) -> ChapterText:
    html = await fetch_html(client, chapter.url)
    return parse_chapter_page(html, chapter.id)
