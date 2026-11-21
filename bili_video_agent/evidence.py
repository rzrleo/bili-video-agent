from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from importlib.util import find_spec
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory, mkdtemp

import requests

from .bilibili import BiliError, BilibiliClient, integer, seconds


@dataclass(frozen=True)
class EvidenceOptions:
    output: Path
    language: str = "auto"
    frames: int = 0
    captions_only: bool = False
    model: str = "base"
    max_minutes: int = 30
    max_media_mib: int = 180


@dataclass
class DownloadBudget:
    remaining: int


def timecode(value: float) -> str:
    minutes, secs = divmod(int(value), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def ffmpeg_path() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise BiliError("抽帧或合并媒体分段需要 FFmpeg。请安装 ffmpeg 或 bili-video-agent[media]。") from exc


def run_ffmpeg(executable: str, args: list[str], cwd: Path) -> None:
    try:
        result = subprocess.run([executable, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
                                cwd=cwd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BiliError(f"FFmpeg 处理失败（{type(exc).__name__}）。") from exc
    if result.returncode:
        raise BiliError(f"FFmpeg 处理失败（退出码 {result.returncode}）。")


def download(client: BilibiliClient, urls: list[str], target: Path, budget: DownloadBudget) -> Path:
    last_error = BiliError("没有可用的 B 站媒体链接。")
    for url in urls:
        try:
            with closing(client.media_response(url, binary=True)) as response:
                expected = integer(response.headers.get("Content-Length"))
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise BiliError("B 站返回了网页或 JSON，无法作为媒体处理。")
                if expected is not None and expected > budget.remaining:
                    raise BiliError("媒体超过下载大小上限。可调整 --max-media-mib。")
                size = 0
                with target.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=128 * 1024):
                        if len(chunk) > budget.remaining:
                            raise BiliError("媒体超过下载大小上限。可调整 --max-media-mib。")
                        budget.remaining -= len(chunk)
                        size += len(chunk)
                        file.write(chunk)
                if size == 0 or (expected and not response.headers.get("Content-Encoding") and size != expected):
                    raise BiliError("B 站返回了空文件或不完整的媒体文件。")
                return target
        except (BiliError, requests.RequestException) as exc:
            target.unlink(missing_ok=True)
            last_error = exc if isinstance(exc, BiliError) else BiliError("媒体下载中断。")
    raise last_error


def track_urls(track: dict) -> list[str]:
    first = track.get("baseUrl") or track.get("base_url") or track.get("url")
    backups = track.get("backupUrl") or track.get("backup_url") or []
    return list(dict.fromkeys(url for url in [first, *backups] if isinstance(url, str) and url))


class MediaFiles:
    def __init__(self, client: BilibiliClient, video: dict, root: Path, max_bytes: int) -> None:
        self.client = client
        self.video = video
        self.root = root
        self.budget = DownloadBudget(max_bytes)
        self.play_info: dict | None = None
        self.shared: Path | None = None

    def get(self, kind: str) -> Path:
        if self.play_info is None:
            self.play_info = self.client.play_info(self.video)
        tracks = (self.play_info.get("dash") or {}).get(kind) or []
        if tracks:
            if kind == "video":
                # Prefer modest resolution and AVC, which is widely decodable.
                tracks = sorted(tracks, key=lambda track: (
                    abs((integer(track.get("height")) or 480) - 480),
                    not str(track.get("codecs", "")).startswith("avc"),
                    integer(track.get("bandwidth")) or 0,
                ))
            else:
                tracks = sorted(tracks, key=lambda track: abs((integer(track.get("bandwidth")) or 80_000) - 80_000))
            urls = [url for track in tracks[:2] for url in track_urls(track)[:3]]
            return download(self.client, urls, self.root / f"{kind}.m4s", self.budget)

        if self.shared is not None:
            return self.shared
        parts = self.play_info.get("durl") or []
        if not parts:
            raise BiliError(f"B 站未提供可下载的{'音频' if kind == 'audio' else '视频'}。")
        paths = [download(self.client, track_urls(part)[:3], self.root / f"segment-{index:03d}.bin", self.budget)
                 for index, part in enumerate(parts)]
        if len(paths) == 1:
            self.shared = paths[0]
        else:
            manifest = self.root / "concat.txt"
            manifest.write_text("".join(f"file '{path.name}'\n" for path in paths), encoding="utf-8")
            combined = self.root / "combined.mkv"
            run_ffmpeg(ffmpeg_path(), ["-f", "concat", "-safe", "1", "-i", manifest.name,
                                      "-c", "copy", combined.name], self.root)
            self.shared = combined
        return self.shared


def transcribe(path: Path, options: EvidenceOptions) -> tuple[list[dict], str | None]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise BiliError("本地语音转写需要安装 bili-video-agent[media]。") from exc
    try:
        model = WhisperModel(options.model, device="cpu", compute_type="int8")
        language = None if options.language == "auto" else options.language
        segments, info = model.transcribe(str(path), language=language, vad_filter=True)
        return [{"start": segment.start, "end": segment.end, "text": segment.text.strip()}
                for segment in segments if segment.text.strip()], info.language
    except Exception as exc:
        # Installed model backends can raise HTTP, decoding, model-loading or inference errors.
        raise BiliError(f"本地 Whisper 转写失败（{type(exc).__name__}）。请检查模型缓存和网络。") from exc


def extract_frames(path: Path, output: Path, length: float, count: int, warnings: list[str]) -> list[dict]:
    executable = ffmpeg_path()
    frames = []
    for index in range(count):
        timestamp = length * (index + 0.5) / count
        target = output / f"frame-{index + 1:02d}.jpg"
        try:
            run_ffmpeg(executable, ["-ss", f"{timestamp:.3f}", "-i", str(path), "-map", "0:v:0",
                                    "-frames:v", "1", "-vf", r"scale=min(720\,iw):-2",
                                    "-q:v", "3", str(target)], output)
        except BiliError as exc:
            target.unlink(missing_ok=True)
            warnings.append(f"{timecode(timestamp)} 抽帧失败。{exc}")
        else:
            if target.is_file() and target.stat().st_size:
                frames.append({"time": round(timestamp, 3), "timestamp": timecode(timestamp), "path": str(target)})
            else:
                warnings.append(f"{timecode(timestamp)} 没有可读画面。")
    return frames


def collect(client: BilibiliClient, video: dict, options: EvidenceOptions) -> dict:
    base = options.output.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    output = Path(mkdtemp(prefix=f"{video['bvid']}-p{video['page']}-", dir=base))
    manifest = output / "evidence.json"
    warnings = []
    segments, language = [], None
    source = None
    frames = []
    try:
        segments, language, login_required = client.captions(video, options.language)
        if segments:
            source = "bilibili_captions"
        elif login_required:
            warnings.append("B 站字幕需要登录。可使用本机 Cookie 文件。")
    except BiliError as exc:
        warnings.append(f"读取 B 站字幕失败。{exc}")

    length = seconds(video.get("duration_seconds"))
    need_asr = not segments and not options.captions_only
    max_seconds = options.max_minutes * 60
    if need_asr and (not length or length > max_seconds):
        warnings.append("分 P 时长未知或超过语音转写上限。已知时长可用 --max-minutes 调整。")
        need_asr = False
    if options.frames and not length:
        warnings.append("分 P 时长未知，无法均匀抽帧。")

    if need_asr or (options.frames and length):
        with TemporaryDirectory(prefix="media-", dir=output) as temp:
            media = MediaFiles(client, video, Path(temp), options.max_media_mib * 1024 * 1024)
            if need_asr:
                try:
                    if find_spec("faster_whisper") is None:
                        raise BiliError("本地语音转写需要安装 bili-video-agent[media]。")
                    segments, language = transcribe(media.get("audio"), options)
                    if segments:
                        source = "whisper"
                    else:
                        warnings.append("没有检测到可转写的语音。")
                except BiliError as exc:
                    warnings.append(str(exc))
            if options.frames and length:
                try:
                    frames = extract_frames(media.get("video"), output, length, options.frames, warnings)
                except BiliError as exc:
                    warnings.append(str(exc))

    transcript = None
    if segments:
        transcript = output / "transcript.txt"
        transcript.write_text("".join(f"[{timecode(cue['start'])}] {cue['text']}\n" for cue in segments),
                              encoding="utf-8")
    if not segments:
        warnings.append("没有可用的字幕或语音转写。")
    status = "unavailable"
    if segments or frames:
        status = "ready" if segments and len(frames) == options.frames else "partial"
    report = {
        "status": status, "video": video, "transcript_source": source, "language": language,
        "transcript_file": str(transcript) if transcript else None,
        "transcript_segments": len(segments), "frames": frames, "warnings": warnings,
        "evidence_file": str(manifest),
    }
    manifest.write_text(json.dumps({**report, "segments": segments}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return report
