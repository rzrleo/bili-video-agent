from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sys

from .bilibili import BiliError, BilibiliClient, ORDERS
from .evidence import EvidenceOptions, collect
from .search import search_videos
from .transcription_worker import MODEL_NAMES


def bounded_integer(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            number = minimum - 1
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"需要介于 {minimum} 和 {maximum} 之间的整数")
        return number

    return parse


def language_code(value: str) -> str:
    value = value.lower()
    if value != "auto" and not re.fullmatch(r"[a-z]{2,3}", value):
        raise argparse.ArgumentTypeError("使用 auto 或语言代码，例如 zh、en、ja")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bili-video", description="搜索 B 站视频并为 Agent 提取总结所需的证据。")
    root.add_argument("--cookies", type=Path, help="可选的本地 Netscape Cookie 文件，也可用 BILI_VIDEO_COOKIES 指定路径")
    commands = root.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="按描述或关键词搜索 B 站视频")
    search.add_argument("query", help="描述或关键词")
    search.add_argument("--keyword", action="append", default=[], help="补充短查询词，最多四个，可重复使用")
    search.add_argument("--limit", type=bounded_integer(1, 50), default=10)
    search.add_argument("--order", choices=ORDERS, default="relevance")

    inspect = commands.add_parser("inspect", help="解析链接并读取分 P 信息")
    evidence = commands.add_parser("evidence", help="提取字幕、语音转写和画面")
    for command in (inspect, evidence):
        command.add_argument("target", help="B 站视频链接、b23.tv 分享链接、BV 号或 av 号")
        command.add_argument("--page", type=bounded_integer(1, 999999), help="覆盖视频链接的分 P")

    evidence.add_argument("--out", type=Path, default=Path("bili-results"), help="证据输出父目录")
    evidence.add_argument("--language", type=language_code, default="auto", help="字幕/转写语言，默认自动")
    evidence.add_argument("--frames", type=bounded_integer(0, 12), default=0, help="抽帧数量，默认 0")
    evidence.add_argument("--captions-only", action="store_true", help="只读取现成字幕，不下载视频或调用 Whisper")
    evidence.add_argument("--model", choices=MODEL_NAMES,
                          default="base", help="本地 Whisper 模型，默认 base")
    evidence.add_argument("--max-minutes", type=bounded_integer(1, 360), default=30, help="允许转写的最大时长，默认 30 分钟")
    evidence.add_argument("--max-media-mib", type=bounded_integer(1, 2048), default=180,
                          help="一次证据任务的媒体下载上限，默认 180 MiB")
    return root


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if args.command == "evidence" and args.captions_only and args.frames:
        command_parser.error("--captions-only 与 --frames 不能同时使用")
    if args.command == "search" and len(args.keyword) > 4:
        command_parser.error("--keyword 最多提供四个")
    cookie_path = os.environ.get("BILI_VIDEO_COOKIES")
    cookies = args.cookies or (Path(cookie_path) if cookie_path else None)
    try:
        with closing(BilibiliClient(cookies)) as client:
            if args.command == "search":
                result = search_videos(client, args.query, args.keyword, args.limit, args.order)
            else:
                video = client.inspect(args.target, args.page)
                result = video if args.command == "inspect" else collect(client, video, EvidenceOptions(
                    output=args.out, language=args.language, frames=args.frames, captions_only=args.captions_only,
                    model=args.model, max_minutes=args.max_minutes, max_media_mib=args.max_media_mib,
                ))
    except (BiliError, OSError) as exc:
        error = str(exc) if isinstance(exc, BiliError) else "无法读取或写入本地文件。请检查路径和权限。"
        print(json.dumps({"status": "error", "error": error}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 3 if result.get("status") == "unavailable" else 0
