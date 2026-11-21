"""Keep native Whisper and media decoding failures out of the CLI process."""

import faulthandler
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

import requests


MODEL_NAMES = ("tiny", "base", "small", "medium", "large-v3", "turbo")


def valid_model(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 1024 * 1024:
        return False
    with path.open("rb") as handle:
        return handle.read(4) == b"lmgg"


def model_path(name: str, directory: Path) -> Path:
    if name not in MODEL_NAMES:
        raise ValueError("Unsupported Whisper model")
    base_name = "large-v3-turbo" if name == "turbo" else name
    filename = f"ggml-{base_name}.bin"
    target = directory.expanduser().resolve() / filename
    if valid_model(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{filename}?download=true"
    partial = None
    try:
        with requests.get(url, stream=True, timeout=(10, 30), headers={"Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Incomplete Whisper model response")
            expected = int(response.headers["Content-Length"]) if "Content-Length" in response.headers else None
            with NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as download:
                partial = Path(download.name)
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    download.write(chunk)
        if not valid_model(partial) or (expected is not None and partial.stat().st_size != expected):
            raise ValueError("Invalid Whisper model download")
        try:
            partial.replace(target)
        except PermissionError:
            if not valid_model(target):
                raise
        return target
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)


def main() -> int:
    if sys.platform == "win32":
        import ctypes

        # A failed native dependency must exit instead of showing a Windows crash dialog.
        fail_critical_errors = 0x0001
        no_error_dialog = 0x0002
        ctypes.windll.kernel32.SetErrorMode(fail_critical_errors | no_error_dialog)
    faulthandler.enable()
    audio, output, model_name, language = sys.argv[1:]
    try:
        from pywhispercpp.constants import MODELS_DIR
        from pywhispercpp.model import Model

        # FFmpeg has already converted the source to the 16 kHz PCM Whisper expects.
        cache = Path(os.environ.get("BILI_VIDEO_MODEL_DIR") or MODELS_DIR)
        model = Model(str(model_path(model_name, cache)),
                      context_params={"use_gpu": False}, print_progress=False)
        if language == "auto":
            (language, _), _ = model.auto_detect_language(audio)
        segments = model.transcribe(audio, language=language)
        result = {
            "language": language,
            "segments": [{"start": segment.t0 / 100, "end": segment.t1 / 100, "text": segment.text.strip()}
                         for segment in segments if segment.text.strip()],
        }
        Path(output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        # Worker errors can include private paths or signed URLs. Return only the error class.
        print(f"asr-error:{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
