"""Keep native Whisper and media decoding failures out of the CLI process."""

import faulthandler
import json
from pathlib import Path
import sys


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
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4)
        requested_language = None if language == "auto" else language
        segments, info = model.transcribe(audio, language=requested_language, vad_filter=True)
        result = {
            "language": info.language,
            "segments": [{"start": segment.start, "end": segment.end, "text": segment.text.strip()}
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
