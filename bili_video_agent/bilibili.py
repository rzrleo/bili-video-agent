from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from html import unescape
from http.cookiejar import MozillaCookieJar
import math
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import requests


API = "https://api.bilibili.com"
WEB = "https://www.bilibili.com"
VIDEO_HOSTS = {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}
SHORT_HOST = "b23.tv"
MEDIA_DOMAINS = {"bilibili.com", "bilivideo.com", "bilivideo.cn", "hdslb.com"}
OTHER_MEDIA_HOSTS = {"upos-hz-mirrorakam.akamaized.net"}
VIDEO_ID = re.compile(r"(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*)", re.IGNORECASE | re.ASCII)
SHARE_URL = re.compile(r"https?://[^\s<>\"'，。；！、《》【】（）]+", re.IGNORECASE)
TAG = re.compile(r"</?[a-z][a-z0-9]*(?:\s[^<>]*)?\s*/?>", re.IGNORECASE)
ORDERS = {"relevance": "totalrank", "latest": "pubdate", "views": "click"}


class BiliError(RuntimeError):
    """A request or video could not be processed."""


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    page: int = 1

    @property
    def url(self) -> str:
        return f"{WEB}/video/{self.video_id}?p={self.page}"


def bvid(value: object) -> str:
    if isinstance(value, str) and VIDEO_ID.fullmatch(value) and value[:2].upper() == "BV":
        return "BV" + value[2:]
    raise BiliError("B 站响应缺少有效的 BV 号。")


def parse_target(value: str) -> VideoRef | str:
    value = value.strip()
    if VIDEO_ID.fullmatch(value):
        prefix = "BV" if value[:2].upper() == "BV" else "av"
        return VideoRef(prefix + value[2:])

    for match in SHARE_URL.finditer(value):
        url = match[0].rstrip(")].!;,")
        try:
            parts = urlsplit(url)
            if parts.username is not None or parts.port is not None:
                continue
        except ValueError:
            continue
        if parts.hostname == SHORT_HOST and re.fullmatch(r"/[0-9A-Za-z]+/?", parts.path):
            return f"https://{SHORT_HOST}{parts.path}"
        if parts.hostname not in VIDEO_HOSTS:
            continue
        path = re.fullmatch(r"/video/([^/]+)/?", parts.path, re.IGNORECASE)
        if path and VIDEO_ID.fullmatch(path[1]):
            page = parse_qs(parts.query, keep_blank_values=True).get("p", ["1"])[0]
            if not re.fullmatch(r"[1-9][0-9]{0,5}", page):
                raise BiliError("分 P 参数必须是正整数。")
            prefix = "BV" if path[1][:2].upper() == "BV" else "av"
            return VideoRef(prefix + path[1][2:], int(page))
    raise BiliError("请输入 B 站视频页、b23.tv 分享链接、BV 号或 av 号。")


def plain(value: object) -> str:
    return unescape(TAG.sub("", str(value or ""))).strip()


def integer(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def seconds(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def duration(value: object) -> int | None:
    if isinstance(value, str) and ":" in value:
        chunks = value.split(":")
        if len(chunks) in (2, 3) and all(re.fullmatch(r"[0-9]{1,4}", part) for part in chunks):
            return sum(int(part) * 60**index for index, part in enumerate(reversed(chunks)))
        return None
    return integer(value)


def allowed_media_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        is_media_host = host in OTHER_MEDIA_HOSTS or any(
            host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS)
        if parts.scheme in {"http", "https"} and parts.username is None and parts.port is None and is_media_host:
            return parts._replace(scheme="https", fragment="").geturl()
    except ValueError:
        pass
    raise BiliError("B 站返回了不受支持的媒体地址。")


class BilibiliClient:
    def __init__(self, cookies: Path | None = None) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"),
            "Referer": WEB + "/",
        })
        if cookies is not None:
            jar = MozillaCookieJar(str(cookies))
            try:
                jar.load()
            except OSError as exc:
                self.session.close()
                raise BiliError("无法读取 Netscape 格式的 B 站 Cookie 文件。") from exc
            for cookie in jar:
                domain = cookie.domain.lstrip(".")
                if domain == "bilibili.com" or domain.endswith(".bilibili.com"):
                    self.session.cookies.set_cookie(cookie)

    def close(self) -> None:
        self.session.close()

    def _get(self, url: str, *, params: dict | None = None, stream: bool = False,
             headers: dict | None = None) -> requests.Response:
        try:
            return self.session.get(url, params=params, headers=headers, stream=stream, timeout=(8, 25),
                                    allow_redirects=False)
        except requests.RequestException as exc:
            raise BiliError(f"连接 B 站失败（{type(exc).__name__}）。") from exc

    @staticmethod
    def _json(response: requests.Response) -> dict:
        if response.status_code != 200:
            raise BiliError(f"B 站请求返回 HTTP {response.status_code}。")
        try:
            data = response.json()
        except ValueError as exc:
            raise BiliError("B 站返回了非 JSON 响应。") from exc
        if not isinstance(data, dict):
            raise BiliError("B 站返回了意外的数据格式。")
        return data

    def _api(self, path: str, params: dict) -> dict:
        with closing(self._get(API + path, params=params)) as response:
            payload = self._json(response)
        if payload.get("code") != 0:
            message = plain(payload.get("message"))[:150]
            raise BiliError(f"B 站接口错误 {payload.get('code')}（{message or '无详情'}）。")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BiliError("B 站响应缺少数据。")
        return data

    def resolve(self, value: str) -> VideoRef:
        target = parse_target(value)
        for _ in range(4):
            if isinstance(target, VideoRef):
                return target
            with closing(self._get(target, stream=True)) as response:
                location = response.headers.get("Location")
                if not response.is_redirect or not location:
                    raise BiliError("B 站分享短链未返回可用的视频地址。")
                target = parse_target(urljoin(target, location))
        if isinstance(target, VideoRef):
            return target
        raise BiliError("B 站分享短链跳转次数过多。")

    def inspect(self, value: str, page: int | None = None) -> dict:
        target = self.resolve(value)
        page = target.page if page is None else page
        params = {"bvid": target.video_id} if target.video_id.startswith("BV") else {"aid": target.video_id[2:]}
        item = self._api("/x/web-interface/wbi/view", params)
        video_id = bvid(item.get("bvid"))
        pages = item.get("pages") or [{"page": 1, "cid": item.get("cid"), "duration": item.get("duration")}]
        selected = next((part for part in pages if part.get("page") == page), None)
        if selected is None:
            raise BiliError(f"视频没有第 {page} P，共 {len(pages)} P。")
        cid = integer(selected.get("cid"))
        if not cid or cid < 0:
            raise BiliError("所选分 P 缺少播放编号。")
        return {
            "bvid": video_id,
            "aid": integer(item.get("aid")),
            "cid": cid,
            "url": VideoRef(video_id, page).url,
            "title": plain(item.get("title")),
            "description": plain(item.get("desc")),
            "author": plain((item.get("owner") or {}).get("name")),
            "published_at": integer(item.get("pubdate")),
            "duration_seconds": duration(selected.get("duration")),
            "page": page,
            "page_title": plain(selected.get("part")),
            "pages": [{"page": part.get("page"), "title": plain(part.get("part")),
                       "duration_seconds": duration(part.get("duration"))} for part in pages],
        }

    def search_page(self, keyword: str, order: str, page: int) -> tuple[list[dict], int]:
        data = self._api("/x/web-interface/wbi/search/type", {
            "search_type": "video", "keyword": keyword, "order": ORDERS[order], "page": page,
        })
        results = []
        for item in data.get("result") or []:
            if not isinstance(item, dict) or item.get("type") not in (None, "video"):
                continue
            try:
                video_id = bvid(item.get("bvid"))
            except BiliError:
                continue
            results.append({
                "bvid": video_id, "url": VideoRef(video_id).url, "title": plain(item.get("title")),
                "description": plain(item.get("description")), "author": plain(item.get("author")),
                "duration_seconds": duration(item.get("duration")), "views": integer(item.get("play")),
                "published_at": integer(item.get("pubdate")),
            })
        return results, integer(data.get("numPages")) or 1

    def captions(self, video: dict, language: str) -> tuple[list[dict], str | None, bool]:
        params = {"bvid": video["bvid"], "cid": video["cid"]}
        data = self._api("/x/player/wbi/v2", params)
        login_required = bool(data.get("need_login_subtitle"))
        tracks = (data.get("subtitle") or {}).get("subtitles") or []
        if language == "auto":
            preferred = ("zh-cn", "zh-hans", "zh", "ai-zh", "zh-tw", "en", "ai-en")
        else:
            preferred = (language, f"ai-{language}")
            tracks = [track for track in tracks if str(track.get("lan", "")).lower()
                      .removeprefix("ai-").split("-")[0] == language]
        priority = {code: rank for rank, code in enumerate(preferred)}
        ordered = sorted(tracks, key=lambda track: priority.get(str(track.get("lan")).lower(), len(priority)))
        last_error = None
        for track in ordered:
            if not track.get("subtitle_url"):
                continue
            try:
                with closing(self.media_response(track["subtitle_url"])) as response:
                    segments = caption_segments(self._json(response).get("body") or [])
            except BiliError as exc:
                last_error = exc
                continue
            if segments:
                return segments, track.get("lan"), login_required
        if last_error is not None:
            raise last_error
        return [], None, login_required

    def play_info(self, video: dict) -> dict:
        return self._api("/x/player/playurl", {
            "bvid": video["bvid"], "cid": video["cid"], "qn": 32, "fnval": 16,
        })

    def media_response(self, url: str, *, binary: bool = False) -> requests.Response:
        headers = {"Range": "bytes=0-", "Accept-Encoding": "identity"} if binary else None
        for _ in range(4):
            url = allowed_media_url(url)
            response = self._get(url, stream=True, headers=headers)
            if response.status_code == 200:
                return response
            if binary and response.status_code == 206:
                content_range = re.fullmatch(r"bytes 0-([0-9]+)/([0-9]+)", response.headers.get("Content-Range", ""))
                if content_range and int(content_range[1]) + 1 == int(content_range[2]):
                    return response
                response.close()
                raise BiliError("B 站 CDN 只返回了媒体片段。")
            location = response.headers.get("Location")
            response.close()
            if not response.is_redirect or not location:
                raise BiliError(f"B 站媒体请求返回 HTTP {response.status_code}。")
            url = urljoin(url, location)
        raise BiliError("B 站媒体地址跳转次数过多。")


def caption_segments(body: list[dict]) -> list[dict]:
    segments = []
    for cue in body:
        if not isinstance(cue, dict):
            continue
        start, end = seconds(cue.get("from")), seconds(cue.get("to"))
        content = plain(cue.get("content"))
        if start is not None and end is not None and start <= end and content:
            segments.append({"start": start, "end": end, "text": content})
    return sorted(segments, key=lambda segment: segment["start"])
