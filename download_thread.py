#!/usr/bin/env python3
"""mirror.chromaso.net 单主题抓取、作者统计与本地筛选后端。"""

from __future__ import annotations

import argparse
import base64
import html
import mimetypes
import re
import sys
import time
import uuid
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


BASE = "https://mirror.chromaso.net"
LOG_LEVELS = ("INFO", "WARNING", "ERROR")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(slots=True)
class Post:
    floor: int
    post_id: str
    author: str
    profile_url: str
    avatar_url: str
    published: str
    body: str
    body_html: str


@dataclass(slots=True)
class Asset:
    url: str
    data: bytes
    mime_type: str


@dataclass(slots=True)
class AuthorWork:
    title: str
    url: str
    forum: str = ""
    reply_count: str = ""
    published: str = ""
    last_updated: str = ""


@dataclass(slots=True)
class AuthorProfileData:
    name: str
    profile_url: str
    avatar_url: str
    works: list[AuthorWork] = field(default_factory=list)
    profile_requires_login: bool = True


@dataclass(slots=True)
class Author:
    name: str
    profile_url: str
    avatar_url: str
    post_count: int = 0

    @property
    def key(self) -> str:
        return self.profile_url or self.name


@dataclass(slots=True)
class ThreadData:
    title: str
    url: str
    posts: list[Post] = field(default_factory=list)
    page_count: int = 0
    assets: dict[str, Asset] = field(default_factory=dict)

    def authors(self) -> list[Author]:
        result: OrderedDict[str, Author] = OrderedDict()
        for post in self.posts:
            key = post.profile_url or post.author
            if key not in result:
                result[key] = Author(post.author, post.profile_url, post.avatar_url, 0)
            result[key].post_count += 1
            if not result[key].avatar_url and post.avatar_url:
                result[key].avatar_url = post.avatar_url
        return list(result.values())

    def posts_by_author(self, author_key: str | None = None) -> list[Post]:
        if not author_key:
            return list(self.posts)
        return [p for p in self.posts if (p.profile_url or p.author) == author_key]

    def posts_by_authors(self, author_keys: set[str] | list[str] | None) -> list[Post]:
        if author_keys is None:
            return list(self.posts)
        keys = set(author_keys)
        return [p for p in self.posts if (p.profile_url or p.author) in keys]


def normalize_url(value: str) -> str:
    """接受主题 ID 或完整主题网址，并移除尾部页码。"""
    value = value.strip()
    if value.isdigit():
        return f"{BASE}/thread/{value}"
    if value.lower().startswith("mirror.chromaso.net/"):
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host != "mirror.chromaso.net":
        raise ValueError("只接受 mirror.chromaso.net 的主题 URL 或纯数字主题 ID")
    match = re.fullmatch(r"/thread/(\d+)(?:/\d+)?/?", parsed.path)
    if not match:
        raise ValueError("URL 格式应为 https://mirror.chromaso.net/thread/主题ID")
    return f"{BASE}/thread/{match.group(1)}"


def _clean_text(node: Tag) -> str:
    clone = BeautifulSoup(str(node), "html.parser")
    for bad in clone.select(
        "script, style, nav, form, button, .actions, .post-actions, .pagination, "
        ".signature, .toolbar, [aria-label='操作']"
    ):
        bad.decompose()
    for br in clone.find_all("br"):
        br.replace_with("\n")
    text = clone.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clean_html(node: Tag, page_url: str) -> str:
    """保留正文富文本属性，同时移除脚本和事件处理器并绝对化链接。"""
    clone_soup = BeautifulSoup(str(node), "html.parser")
    clone = clone_soup.find()
    if clone is None:
        return ""
    for bad in clone.select("script, iframe, object, embed, form, button"):
        bad.decompose()
    for element in clone.find_all(True):
        for attr in list(element.attrs):
            if attr.lower().startswith("on"):
                del element.attrs[attr]
        if element.get("style"):
            allowed_styles = {
                "color",
                "background-color",
                "font-size",
                "font-family",
                "font-style",
                "font-weight",
                "text-decoration",
                "text-decoration-line",
                "text-align",
                "line-height",
                "letter-spacing",
                "vertical-align",
            }
            declarations = []
            for declaration in str(element["style"]).split(";"):
                if ":" not in declaration:
                    continue
                name, value = declaration.split(":", 1)
                if name.strip().lower() in allowed_styles:
                    declarations.append(f"{name.strip().lower()}:{value.strip()}")
            if declarations:
                element["style"] = ";".join(declarations)
            else:
                element.attrs.pop("style", None)
        if element.name == "a" and element.get("href"):
            raw_href = str(element["href"]).strip()
            absolute_href = urljoin(page_url, raw_href)
            scheme = urlparse(absolute_href).scheme.lower()
            if raw_href.startswith("#") or scheme in {"http", "https", "mailto"}:
                element["href"] = absolute_href
                element["rel"] = "noopener noreferrer"
                element["target"] = "_blank"
            else:
                element.attrs.pop("href", None)
        if element.name in {"img", "source", "video", "audio"}:
            source = element.get("src") or element.get("data-src") or element.get("data-original")
            if source:
                element["src"] = urljoin(page_url, str(source))
            element.attrs.pop("data-src", None)
            element.attrs.pop("data-original", None)
            if element.name == "img":
                element.attrs.pop("width", None)
                element.attrs.pop("height", None)
        if element.get("srcset"):
            rewritten = []
            for candidate in str(element["srcset"]).split(","):
                parts = candidate.strip().split()
                if parts:
                    parts[0] = urljoin(page_url, parts[0])
                    rewritten.append(" ".join(parts))
            element["srcset"] = ", ".join(rewritten)
    return "".join(str(child) for child in clone.contents).strip()


def _find_posts(soup: BeautifulSoup) -> list[Tag]:
    selectors = (
        ".mm-post",
        "[data-post-id]",
        "article.post",
        ".thread-post",
        ".post-container",
        ".post",
        "main article",
    )
    for selector in selectors:
        nodes = [n for n in soup.select(selector) if isinstance(n, Tag)]
        nodes = [n for n in nodes if not any(parent in nodes for parent in n.parents)]
        if nodes:
            return nodes
    return []


def _first_attr(node: Tag | None, names: tuple[str, ...]) -> str:
    if not node:
        return ""
    for name in names:
        value = node.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _parse_post(post: Tag, floor: int, page_url: str) -> Post:
    author_node = post.select_one(
        "a.ui-link[href^='/author/'], .card-header a[href^='/author/']:not(.mm-post-ava), "
        ".author a, .username a, a.username, [itemprop='author'] a, "
        "a[href*='/user/'], a[href*='/profile/']"
    )
    if author_node is None:
        author_node = post.select_one(".author, .username, [data-user-name], [itemprop='author']")
    author = _clean_text(author_node) if author_node else "未知作者"
    profile_url = urljoin(page_url, _first_attr(author_node, ("href",)))
    if profile_url == page_url:
        profile_url = ""

    avatar_node = post.select_one(
        "img.mm-img-ava, img.avatar, .avatar img, img.user-avatar, "
        "img[src*='avatar'], [style*='background-image']"
    )
    avatar_url = _first_attr(avatar_node, ("src", "data-src", "data-original"))
    if not avatar_url and avatar_node:
        style = _first_attr(avatar_node, ("style",))
        match = re.search(r"url\(['\"]?([^)'\"]+)", style)
        avatar_url = match.group(1) if match else ""
    avatar_url = urljoin(page_url, avatar_url) if avatar_url else ""

    time_node = post.select_one("time, .date, .timestamp, [datetime]")
    published = _first_attr(time_node, ("datetime", "title"))
    if not published and time_node:
        published = _clean_text(time_node)
    body_node = post.select_one(
        ".card-body, .post-content, .content, .message, "
        "[itemprop='articleBody'], .markdown-body"
    )
    body = _clean_text(body_node or post)
    body_html = _clean_html(body_node or post, page_url)
    post_id = _first_attr(post, ("data-post-id", "id"))
    return Post(
        floor, post_id, author, profile_url, avatar_url, published, body, body_html
    )


class ThreadDownloader:
    """可复用的下载器；同一个实例也负责获取头像字节。"""

    def __init__(self, timeout: int = 20, logger=None) -> None:
        self.timeout = timeout
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

    def _log(self, level: str, message: str) -> None:
        if self.logger:
            try:
                self.logger(level if level in LOG_LEVELS else "INFO", message)
            except Exception:
                pass

    def _get_soup(self, url: str) -> BeautifulSoup:
        self._log("INFO", f"GET {url}")
        try:
            response = self.session.get(url, timeout=self.timeout)
            self._log("INFO", f"HTTP {response.status_code}，响应 {len(response.content)} 字节")
            response.raise_for_status()
        except requests.RequestException as exc:
            self._log("ERROR", f"请求失败：{exc}")
            raise
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding
        return BeautifulSoup(response.text, "html.parser")

    def fetch(
        self,
        thread: str,
        page_delay: float = 0.8,
        max_pages: int = 1000,
        progress=None,
    ) -> ThreadData:
        """先抓取完整主题；page_delay 是每次翻页前的等待秒数。"""
        thread_url = normalize_url(thread)
        data = ThreadData("未命名主题", thread_url)
        seen: set[str] = set()
        thread_id = thread_url.rstrip("/").rsplit("/", 1)[-1]
        next_url: str | None = thread_url
        current_offset = 0
        self._log("INFO", f"开始读取主题 {thread_url}；翻页间隔 {page_delay:g} 秒")

        for page_number in range(1, max_pages + 1):
            if not next_url:
                break
            if page_number > 1 and page_delay > 0:
                self._log("INFO", f"等待 {page_delay:g} 秒后读取下一页")
                time.sleep(page_delay)
            page_url = next_url
            soup = self._get_soup(page_url)
            if page_number == 1:
                h1 = soup.select_one("h1")
                data.title = _clean_text(h1) if h1 else (
                    soup.title.get_text(strip=True) if soup.title else data.title
                )
                data.title = re.sub(r"^M系镜像\s*[-–—]\s*", "", data.title)
                self._log("INFO", f"主题标题：{data.title}")

            nodes = _find_posts(soup)
            self._log("INFO", f"第 {page_number} 页匹配到 {len(nodes)} 个帖子节点")
            if not nodes:
                if page_number == 1:
                    self._log("ERROR", "页面中没有匹配到 .mm-post 帖子节点")
                    raise RuntimeError("页面中未找到帖子；页面可能要求登入，或网站结构已经变化")
                self._log("WARNING", "当前分页没有帖子节点，停止翻页")
                break

            new_count = 0
            for node in nodes:
                parsed = _parse_post(node, len(data.posts) + 1, page_url)
                key = parsed.post_id or "|".join(
                    (parsed.profile_url or parsed.author, parsed.published, parsed.body[:500])
                )
                if key not in seen:
                    seen.add(key)
                    data.posts.append(parsed)
                    new_count += 1

            data.page_count += 1
            self._log(
                "INFO",
                f"第 {page_number} 页新增 {new_count} 条，累计 {len(data.posts)} 条发言",
            )
            if progress:
                progress(page_number, len(data.posts))

            # 该站分页不是 /2、/3，而是 /+20、/+40 形式的帖子偏移量。
            offsets: list[tuple[int, str]] = []
            offset_pattern = re.compile(rf"^/thread/{re.escape(thread_id)}/\+(\d+)(?:[?#].*)?$")
            for link in soup.select("a[href]"):
                href = str(link.get("href", ""))
                match = offset_pattern.match(href)
                if match:
                    offset = int(match.group(1))
                    if offset > current_offset:
                        offsets.append((offset, urljoin(page_url, href)))
            if new_count == 0:
                self._log("WARNING", "本页没有新增内容，为避免循环已停止")
                break
            if offsets:
                current_offset, next_url = min(offsets, key=lambda item: item[0])
                self._log("INFO", f"发现下一分页：偏移量 +{current_offset}")
            else:
                next_url = None
                self._log("INFO", "未发现下一分页，主题读取完成")
        else:
            self._log("ERROR", f"达到最大页数限制 {max_pages}")
            raise RuntimeError(f"已达到最大页数 {max_pages}，为防止无限请求已停止")
        self._fetch_post_assets(data)
        self._log(
            "INFO",
            f"主题读取结束：{data.page_count} 页、{len(data.posts)} 条发言、"
            f"{len(data.authors())} 位作者、{len(data.assets)} 个正文资源",
        )
        return data

    def _fetch_post_assets(self, data: ThreadData) -> None:
        urls: list[str] = []
        seen: set[str] = set()
        for post in data.posts:
            fragment = BeautifulSoup(post.body_html, "html.parser")
            for image in fragment.select("img[src]"):
                url = str(image.get("src", "")).strip()
                if url and not url.startswith("data:") and url not in seen:
                    seen.add(url)
                    urls.append(url)
        if not urls:
            self._log("INFO", "正文中没有需要下载的图片")
            return

        self._log("INFO", f"开始获取正文图片，共 {len(urls)} 个")
        total_size = 0
        max_item_size = 50 * 1024 * 1024
        max_total_size = 500 * 1024 * 1024
        for index, url in enumerate(urls, 1):
            try:
                response = self.session.get(url, timeout=self.timeout, stream=True)
                response.raise_for_status()
                expected = int(response.headers.get("content-length", "0") or 0)
                if expected > max_item_size or total_size + expected > max_total_size:
                    self._log("WARNING", f"图片过大，已跳过：{url}")
                    continue
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_item_size or total_size + size > max_total_size:
                        chunks = []
                        break
                    chunks.append(chunk)
                if not chunks:
                    self._log("WARNING", f"图片为空或超过大小限制，已跳过：{url}")
                    continue
                raw = b"".join(chunks)
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip()
                if not mime.startswith("image/"):
                    mime = mimetypes.guess_type(urlparse(url).path)[0] or "application/octet-stream"
                data.assets[url] = Asset(url, raw, mime)
                total_size += len(raw)
                self._log("INFO", f"图片 {index}/{len(urls)} 已获取：{len(raw)} 字节")
            except requests.RequestException as exc:
                self._log("WARNING", f"正文图片获取失败 {url}：{exc}")

    def fetch_author_profile(
        self,
        author_name: str,
        profile_url: str,
        avatar_url: str = "",
        page_delay: float = 0.8,
        max_pages: int = 1000,
        progress=None,
    ) -> AuthorProfileData:
        """获取公开可见的作者主题作品；作者资料页本身可能要求登入。"""
        if not author_name.strip():
            raise ValueError("作者名称不能为空")
        profile = AuthorProfileData(author_name.strip(), profile_url, avatar_url)
        query_parameters = {
            'type': 'thread',
            'query': '',
            'user': profile.name,
            'forum': '0',
            'sort': 'reply',
        }
        query_url = f"{BASE}/query?{urlencode(query_parameters)}"
        self._log("INFO", f"搜索作者主题：{profile.name}")
        soup = self._get_soup(query_url)
        result_link = soup.find("a", href=re.compile(r"^/search/\d+/?$"))
        if not result_link:
            raise RuntimeError("网站没有返回作者作品搜索结果地址")
        result_url = urljoin(query_url, str(result_link.get("href", "")))
        current_offset = 0
        seen_urls: set[str] = set()

        for page_number in range(1, max_pages + 1):
            # 搜索任务由站点异步生成，短暂轮询等待表格出现。
            for attempt in range(12):
                soup = self._get_soup(result_url)
                if soup.select_one("table tbody") or "搜索进行中" not in soup.get_text():
                    break
                time.sleep(0.5)
            rows = soup.select("table tbody tr")
            new_count = 0
            last_work: AuthorWork | None = None
            for row in rows:
                cells = row.find_all("td", recursive=False)
                thread_link = row.find("a", href=re.compile(r"^/thread/\d+/?$"))
                if not thread_link:
                    continue
                url = urljoin(result_url, str(thread_link.get("href", "")))
                if len(cells) >= 4:
                    if url in seen_urls:
                        continue
                    title = _clean_text(thread_link)
                    forum_text = _clean_text(cells[1])
                    forum = forum_text[len(title):].strip() if forum_text.startswith(title) else ""
                    updated_node = cells[3].select_one("time")
                    last_updated = (
                        _first_attr(updated_node, ("datetime", "title"))
                        or (_clean_text(updated_node) if updated_node else "")
                    )
                    work = AuthorWork(
                        title=title,
                        url=url,
                        forum=forum,
                        reply_count=_clean_text(cells[2]),
                        last_updated=last_updated,
                    )
                    profile.works.append(work)
                    seen_urls.add(url)
                    last_work = work
                    new_count += 1
                elif last_work and url == last_work.url:
                    time_node = row.select_one("time")
                    last_work.published = (
                        _first_attr(time_node, ("datetime", "title"))
                        or (_clean_text(time_node) if time_node else "")
                    )

            self._log(
                "INFO",
                f"作者作品第 {page_number} 页新增 {new_count} 个，累计 {len(profile.works)} 个",
            )
            if progress:
                progress(page_number, len(profile.works))

            search_id_match = re.search(r"/search/(\d+)", result_url)
            if not search_id_match:
                break
            search_id = search_id_match.group(1)
            offsets: list[tuple[int, str]] = []
            pattern = re.compile(rf"^/search/{search_id}/\+(\d+)(?:[?#].*)?$")
            for link in soup.select("a[href]"):
                href = str(link.get("href", ""))
                match = pattern.match(href)
                if match and int(match.group(1)) > current_offset:
                    offsets.append((int(match.group(1)), urljoin(result_url, href)))
            if not offsets:
                break
            current_offset, result_url = min(offsets, key=lambda item: item[0])
            if page_delay > 0:
                time.sleep(page_delay)
        else:
            raise RuntimeError(f"作者作品已达到最大页数 {max_pages}")

        self._log("INFO", f"作者 {profile.name} 共找到 {len(profile.works)} 个公开主题")
        return profile

    def avatar_bytes(self, url: str, max_size: int = 2_000_000) -> bytes:
        if not url:
            return b""
        try:
            response = self.session.get(url, timeout=self.timeout, stream=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            self._log("WARNING", f"头像加载失败 {url}：{exc}")
            raise
        length = int(response.headers.get("content-length", "0") or 0)
        if length > max_size:
            self._log("WARNING", f"头像超过 {max_size} 字节，已跳过：{url}")
            return b""
        content = response.content
        return content if len(content) <= max_size else b""


def render_markdown(data: ThreadData, author_key: str | None = None) -> str:
    posts = data.posts_by_author(author_key)
    selected = next((a for a in data.authors() if a.key == author_key), None)
    lines = [f"# {data.title}", "", f"- 来源：{data.url}"]
    if selected:
        lines += [f"- 作者筛选：{selected.name}", f"- 作者主页：{selected.profile_url or '无'}"]
    lines += [f"- 收录：{len(posts)} 帖（原主题共 {len(data.posts)} 帖/{data.page_count} 页）", ""]
    for post in posts:
        lines += [
            f"## 原主题第 {post.floor} 楼",
            "",
            f"**作者：** [{post.author}]({post.profile_url})" if post.profile_url else f"**作者：** {post.author}",
            f"**时间：** {post.published}" if post.published else "",
            "",
            post.body,
            "",
            "---",
            "",
        ]
    return "\n".join(line for line in lines).rstrip() + "\n"


def save_thread(data: ThreadData, output: Path | str, author_key: str | None = None) -> int:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    posts = data.posts_by_author(author_key)
    path.write_text(render_markdown(data, author_key), encoding="utf-8")
    return len(posts)


EXPORT_CSS = """
body { margin: 2rem auto; max-width: 920px; padding: 0 1rem; color: #202124;
       background: #fff; font-family: Arial, 'Microsoft YaHei', sans-serif; line-height: 1.75; }
h1 { line-height: 1.3; margin-bottom: .5rem; }
.thread-meta { color: #667085; margin-bottom: 1.5rem; }
.post { border: 1px solid #d9e2ec; border-radius: 10px; margin: 0 0 1rem;
        padding: 1rem 1.15rem; break-inside: avoid; }
.post-header { border-bottom: 1px solid #edf0f2; margin-bottom: .8rem; padding-bottom: .55rem; }
.post-floor { color: #1677c8; font-weight: 700; }
.post-author { font-weight: 700; margin-left: .8rem; }
.post-time { color: #667085; float: right; }
.post-body { overflow-wrap: anywhere; }
.post-body img { height: auto; max-width: 100%; }
.post-body a { color: #0067c0; text-decoration: underline; }
.post-body blockquote { border-left: 4px solid #9dc8ef; color: #48576a; margin-left: 0;
                        padding: .4rem .8rem; background: #f5f9fd; }
.post-body pre { white-space: pre-wrap; }
@media print { body { max-width: none; margin: 0; } .post { break-inside: avoid; } }
"""

PDF_CSS = """
body { color: #202124; font-family: 'Microsoft YaHei', Arial, sans-serif;
       font-size: 11pt; line-height: 1.55; }
h1 { font-size: 20pt; margin: 0 0 8pt; }
.thread-meta { color: #667085; margin-bottom: 14pt; }
.post { border: 1px solid #d9e2ec; margin: 0 0 12pt; padding: 9pt; }
.post-header { border-bottom: 1px solid #edf0f2; margin-bottom: 7pt; padding-bottom: 5pt; }
.post-floor { color: #1677c8; font-weight: bold; }
.post-author { font-weight: bold; margin-left: 10pt; }
.post-time { color: #667085; margin-left: 10pt; }
.post-body { word-break: normal; }
.post-body img { height: auto; }
.post-body a { color: #0067c0; text-decoration: underline; }
.post-body blockquote { border-left: 3px solid #9dc8ef; color: #48576a;
                        margin-left: 0; padding-left: 8pt; }
"""

EPUB_CSS = """
body { margin: 5%; color: #202124; font-family: sans-serif; line-height: 1.6;
       text-align: left; word-break: normal; }
h1 { line-height: 1.3; }
.thread-meta { color: #667085; margin-bottom: 1.2em; }
.post { border-bottom: 1px solid #d9e2ec; margin: 0 0 1.2em; padding: 0 0 1em; }
.post-header { margin-bottom: .7em; }
.post-floor { color: #1677c8; font-weight: bold; }
.post-author { font-weight: bold; margin-left: .7em; }
.post-time { color: #667085; margin-left: .7em; }
.post-body { display: block; width: auto; max-width: none; word-break: normal; }
.post-body img { display: block; width: auto; height: auto; max-width: 100%; margin: .6em auto; }
.post-body a { color: #0067c0; text-decoration: underline; }
.post-body blockquote { border-left: 3px solid #9dc8ef; margin-left: 0; padding-left: .7em; }
"""


def _selected_posts(data: ThreadData, author_keys: set[str] | list[str]) -> list[Post]:
    keys = set(author_keys)
    if not keys:
        raise ValueError("至少选择一位发帖人后才能导出")
    posts = data.posts_by_authors(keys)
    if not posts:
        raise ValueError("没有找到所选发帖人的内容")
    return posts


def _used_asset_urls(data: ThreadData, author_keys: set[str] | list[str]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for post in _selected_posts(data, author_keys):
        fragment = BeautifulSoup(post.body_html, "html.parser")
        for image in fragment.select("img[src]"):
            url = str(image.get("src", ""))
            if url in data.assets and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _asset_extension(asset: Asset) -> str:
    known = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }
    return known.get(asset.mime_type, mimetypes.guess_extension(asset.mime_type) or ".bin")


def _rewrite_post_images(body_html: str, resolver) -> str:
    fragment = BeautifulSoup(body_html, "html.parser")
    for image in fragment.select("img[src]"):
        original = str(image.get("src", ""))
        replacement = resolver(original)
        width = None
        if isinstance(replacement, tuple):
            replacement, width = replacement
        if replacement:
            image["src"] = replacement
            image.attrs.pop("srcset", None)
            image.attrs.pop("height", None)
            if width:
                image["width"] = str(width)
                image["style"] = "height:auto"
            else:
                image.attrs.pop("width", None)
                image["style"] = "max-width:100%;height:auto"
    return str(fragment)


def build_html_document(
    data: ThreadData,
    author_keys: set[str] | list[str],
    image_resolver=None,
    xhtml: bool = False,
    css: str = EXPORT_CSS,
) -> str:
    posts = _selected_posts(data, author_keys)
    resolver = image_resolver or (lambda url: url)
    articles: list[str] = []
    for post in posts:
        profile = (
            f'<a href="{html.escape(post.profile_url, quote=True)}">{html.escape(post.author)}</a>'
            if post.profile_url
            else html.escape(post.author)
        )
        body = _rewrite_post_images(post.body_html, resolver)
        articles.append(
            '<article class="post">'
            '<header class="post-header">'
            f'<span class="post-floor">原主题第 {post.floor} 楼</span>'
            f'<span class="post-author">{profile}</span>'
            f'<span class="post-time">{html.escape(post.published)}</span>'
            '</header>'
            f'<div class="post-body">{body}</div>'
            '</article>'
        )
    prefix = '<?xml version="1.0" encoding="utf-8"?>\n' if xhtml else '<!doctype html>\n'
    namespace = ' xmlns="http://www.w3.org/1999/xhtml"' if xhtml else ""
    return (
        f'{prefix}<html{namespace} lang="zh-CN"><head><meta charset="utf-8"/>'
        f'<title>{html.escape(data.title)}</title><style>{css}</style></head><body>'
        f'<h1>{html.escape(data.title)}</h1>'
        f'<div class="thread-meta">来源：<a href="{html.escape(data.url, quote=True)}">'
        f'{html.escape(data.url)}</a> · 导出 {len(posts)} 条发言</div>'
        + "".join(articles)
        + "</body></html>"
    )


def _standalone_html(data: ThreadData, author_keys: set[str] | list[str]) -> str:
    def resolve(url: str) -> str:
        asset = data.assets.get(url)
        if not asset:
            return url
        encoded = base64.b64encode(asset.data).decode("ascii")
        return f"data:{asset.mime_type};base64,{encoded}"

    return build_html_document(data, author_keys, resolve)


def render_post_body_html(
    data: ThreadData, post: Post, max_image_width: int | None = None
) -> str:
    """生成适合界面预览的单帖富文本，正文图片以内嵌资源呈现。"""
    def resolve(url: str) -> str:
        asset = data.assets.get(url)
        if not asset:
            return url
        encoded = base64.b64encode(asset.data).decode("ascii")
        source = f"data:{asset.mime_type};base64,{encoded}"
        if max_image_width:
            try:
                from PyQt5.QtGui import QImage

                image = QImage.fromData(asset.data)
                if not image.isNull():
                    return source, min(image.width(), max_image_width)
            except Exception:
                pass
        return source

    return _rewrite_post_images(post.body_html, resolve)


def _safe_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return (name[:120] or "thread")


def _export_pdf(data: ThreadData, path: Path, author_keys: set[str] | list[str]) -> None:
    from PyQt5.QtCore import Qt, QUrl
    from PyQt5.QtGui import QImage, QTextDocument
    from PyQt5.QtPrintSupport import QPrinter
    from PyQt5.QtWidgets import QApplication

    if QApplication.instance() is None:
        raise RuntimeError("PDF 导出需要在图形界面程序中运行")
    resource_names: dict[str, tuple[str, int]] = {}
    document = QTextDocument()
    for index, url in enumerate(_used_asset_urls(data, author_keys)):
        asset = data.assets[url]
        name = f"asset://image/{index}"
        image = QImage.fromData(asset.data)
        if not image.isNull():
            max_width = 620
            if image.width() > max_width:
                image = image.scaledToWidth(max_width, Qt.SmoothTransformation)
            resource_names[url] = (name, max(1, image.width()))
            document.addResource(QTextDocument.ImageResource, QUrl(name), image)
    document.setHtml(
        build_html_document(
            data,
            author_keys,
            lambda url: resource_names.get(url, (url, None)),
            css=PDF_CSS,
        )
    )
    printer = QPrinter(QPrinter.HighResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setOutputFileName(str(path))
    printer.setPageSize(QPrinter.A4)
    printer.setFullPage(False)
    document.print_(printer)


def _export_epub(data: ThreadData, path: Path, author_keys: set[str] | list[str]) -> None:
    def epub_asset(asset: Asset) -> tuple[bytes, str]:
        if asset.mime_type in {"image/jpeg", "image/png", "image/gif", "image/svg+xml"}:
            return asset.data, asset.mime_type
        try:
            from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
            from PyQt5.QtGui import QImage

            image = QImage.fromData(asset.data)
            if image.isNull():
                return asset.data, asset.mime_type
            encoded = QByteArray()
            buffer = QBuffer(encoded)
            buffer.open(QIODevice.WriteOnly)
            if image.save(buffer, "PNG"):
                return bytes(encoded), "image/png"
        except Exception:
            pass
        return asset.data, asset.mime_type

    asset_names: dict[str, str] = {}
    epub_assets: dict[str, tuple[bytes, str]] = {}
    manifest_images: list[tuple[str, str, str]] = []
    for index, url in enumerate(_used_asset_urls(data, author_keys)):
        asset = data.assets[url]
        raw, mime = epub_asset(asset)
        converted = Asset(url, raw, mime)
        name = f"assets/image-{index}{_asset_extension(converted)}"
        asset_names[url] = name
        epub_assets[url] = (raw, mime)
        manifest_images.append((f"image-{index}", name, mime))

    chapter = build_html_document(
        data,
        author_keys,
        lambda url: asset_names.get(url, url),
        xhtml=True,
        css=EPUB_CSS,
    )
    book_id = f"urn:uuid:{uuid.uuid4()}"
    manifest = "".join(
        f'<item id="{item_id}" href="{html.escape(name, quote=True)}" '
        f'media-type="{html.escape(mime, quote=True)}"/>'
        for item_id, name, mime in manifest_images
    )
    package = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{book_id}</dc:identifier>
    <dc:title>{html.escape(data.title)}</dc:title>
    <dc:language>zh-CN</dc:language>
    <meta property="dcterms:modified">{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
    {manifest}
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>'''
    nav = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="zh-CN"><head><title>目录</title></head>
<body><nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc">
<ol><li><a href="chapter.xhtml">{html.escape(data.title)}</a></li></ol>
</nav></body></html>'''
    container = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/content.opf", package, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("EPUB/chapter.xhtml", chapter, compress_type=zipfile.ZIP_DEFLATED)
        for url, name in asset_names.items():
            archive.writestr(f"EPUB/{name}", epub_assets[url][0], compress_type=zipfile.ZIP_DEFLATED)


def export_thread(
    data: ThreadData,
    output_directory: Path | str,
    output_format: str,
    author_keys: set[str] | list[str],
) -> Path:
    """按所选作者导出富文本主题；支持 HTML、PDF 和 EPUB。"""
    _selected_posts(data, author_keys)
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    fmt = output_format.strip().upper()
    if fmt == "FDF":  # 兼容常见输入笔误
        fmt = "PDF"
    if fmt not in {"HTML", "PDF", "EPUB"}:
        raise ValueError(f"不支持的导出格式：{output_format}")
    path = directory / f"{_safe_filename(data.title)}.{fmt.lower()}"
    if fmt == "HTML":
        path.write_text(_standalone_html(data, author_keys), encoding="utf-8")
    elif fmt == "PDF":
        _export_pdf(data, path, author_keys)
    else:
        _export_epub(data, path, author_keys)
    return path


def download_thread(
    thread_url: str,
    output: Path,
    delay: float = 0.8,
    timeout: int = 20,
    max_pages: int = 1000,
    author_key: str | None = None,
) -> tuple[str, int, int]:
    """兼容旧调用：完整抓取后，可按 author_key 本地筛选并保存。"""
    data = ThreadDownloader(timeout).fetch(thread_url, delay, max_pages)
    count = save_thread(data, output, author_key)
    return data.title, count, data.page_count


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 M系镜像单个主题并可按作者筛选")
    parser.add_argument("thread", help="主题 URL 或主题 ID")
    parser.add_argument("-o", "--output", type=Path, default=Path("thread.md"))
    parser.add_argument("--delay", type=float, default=0.8, help="下载翻页间隔秒数")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--list-authors", action="store_true", help="列出作者，不保存正文")
    parser.add_argument("--author", help="只保存作者名或作者主页 URL 对应的发言")
    args = parser.parse_args()
    try:
        data = ThreadDownloader(args.timeout).fetch(args.thread, max(args.delay, 0), args.max_pages)
        if args.list_authors:
            for author in data.authors():
                print(f"{author.name}\t{author.post_count}\t{author.profile_url}\t{author.avatar_url}")
            return 0
        author_key = None
        if args.author:
            author = next(
                (a for a in data.authors() if args.author in {a.name, a.profile_url, a.key}), None
            )
            if not author:
                raise ValueError(f"主题中没有找到作者：{args.author}")
            author_key = author.key
        count = save_thread(data, args.output, author_key)
        print(f"完成：{data.title}（保存 {count} 帖/{data.page_count} 页） -> {args.output.resolve()}")
        return 0
    except (ValueError, requests.RequestException, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
