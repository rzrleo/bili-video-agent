from contextlib import closing
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

from bili_video_agent.bilibili import BiliError, BilibiliClient, VideoRef, allowed_media_url, parse_target
from bili_video_agent.evidence import EvidenceOptions, collect, transcribe
from bili_video_agent.search import search_videos
from bili_video_agent.transcription_worker import model_path


VIDEO_ID = "BV1xx411c7mD"


class LinksTest(unittest.TestCase):
    def test_share_text_preserves_selected_page(self):
        self.assertEqual(parse_target(f"看看这个 https://www.bilibili.com/video/{VIDEO_ID}?p=2。"),
                         VideoRef(VIDEO_ID, 2))
        self.assertEqual(parse_target("av12345"), VideoRef("av12345"))
        self.assertEqual(parse_target("https://b23.tv/Abc1?share=1"), "https://b23.tv/Abc1")

    def test_short_link_does_not_request_unrelated_origin(self):
        response = Mock(is_redirect=True, headers={"Location": f"https://example.org/video/{VIDEO_ID}"})
        with closing(BilibiliClient()) as client, patch.object(client, "_get", return_value=response) as get:
            with self.assertRaises(BiliError):
                client.resolve("https://b23.tv/Abc1")
            get.assert_called_once_with("https://b23.tv/Abc1", stream=True)

    def test_media_urls_are_limited_to_bilibili_hosts(self):
        self.assertEqual(allowed_media_url("//aisubtitle.hdslb.com/a"), "https://aisubtitle.hdslb.com/a")
        with self.assertRaises(BiliError):
            allowed_media_url("https://bilivideo.com.example.org/a")

    def test_part_duration_is_not_total_video_duration(self):
        data = {"bvid": VIDEO_ID, "duration": 40, "pages": [
            {"page": 1, "cid": 123, "duration": 10}, {"page": 2, "cid": 456, "duration": 30},
        ]}
        with closing(BilibiliClient()) as client, patch.object(client, "_api", return_value=data):
            result = client.inspect(f"https://www.bilibili.com/video/{VIDEO_ID}?p=2")
        self.assertEqual((result["page"], result["cid"], result["duration_seconds"]), (2, 456, 30))


class WorkflowsTest(unittest.TestCase):
    def test_search_fuses_queries_and_survives_one_failed_query(self):
        first = {"bvid": VIDEO_ID}
        second = {"bvid": "BV1GJ411x7h7"}
        with closing(BilibiliClient()) as client, patch.object(client, "search_page", side_effect=[
            ([second, first], 1), BiliError("test failure"), ([first], 1),
        ]):
            result = search_videos(client, "description", ["first", "bad"], 5, "relevance")
        self.assertEqual(result["results"], [
            {**first, "matched_queries": ["first", "description"]}, {**second, "matched_queries": ["first"]},
        ])
        self.assertEqual(len(result["warnings"]), 1)

    def test_subtitles_skip_download_and_are_saved_with_timestamps(self):
        video = {"bvid": VIDEO_ID, "page": 1, "duration_seconds": 8}
        cues = [{"start": 1.25, "end": 4.5, "text": "示例转写"}]
        with TemporaryDirectory() as root, closing(BilibiliClient()) as client:
            with patch.object(client, "captions", return_value=(cues, "zh-CN", False)), \
                    patch.object(client, "play_info") as play_info:
                result = collect(client, video, EvidenceOptions(Path(root)))
            play_info.assert_not_called()
            self.assertEqual(result["status"], "ready")
            self.assertEqual(Path(result["transcript_file"]).read_text(encoding="utf-8"), "[00:00:01] 示例转写\n")
            self.assertEqual(json.loads(Path(result["evidence_file"]).read_text(encoding="utf-8"))["segments"], cues)

    def test_metadata_is_not_treated_as_evidence(self):
        video = {"bvid": VIDEO_ID, "page": 1, "duration_seconds": 8, "title": "Only a title"}
        with TemporaryDirectory() as root, closing(BilibiliClient()) as client:
            with patch.object(client, "captions", return_value=([], None, True)):
                result = collect(client, video, EvidenceOptions(Path(root), captions_only=True))
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["transcript_file"])

    def test_native_access_violation_is_reported_to_the_parent(self):
        exit_status = subprocess.CompletedProcess([], 0xC0000005, b"", b"Windows fatal exception")
        with patch("bili_video_agent.evidence.ffmpeg_path", return_value="ffmpeg"), \
                patch("bili_video_agent.evidence.run_ffmpeg"), \
                patch("bili_video_agent.evidence.subprocess.run", return_value=exit_status):
            with self.assertRaisesRegex(BiliError, "Windows.*访问冲突"):
                transcribe(Path("audio.m4s"), EvidenceOptions(Path(".")))

    def test_incomplete_model_is_not_published_to_cache(self):
        response = MagicMock(status_code=200, headers={"Content-Length": str(2 * 1024 * 1024)})
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"lmgg" + bytes(1024 * 1024)]
        with TemporaryDirectory() as root, patch("bili_video_agent.transcription_worker.requests.get",
                                                return_value=response):
            directory = Path(root)
            with self.assertRaisesRegex(ValueError, "Invalid Whisper model download"):
                model_path("tiny", directory)
            self.assertEqual(list(directory.iterdir()), [])
