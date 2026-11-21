# Bili Video Agent

给 Codex 和其他 Agent 用的 B 站视频检索工具。输入需求描述得到候选视频，输入链接得到用于总结的带时间戳字幕、语音转写和关键帧。以 Python CLI 提供 JSON，仓库同时附带 Codex Skill。

## 安装

需要 Python 3.10 或更新版本。Windows、macOS 和 Linux 可使用相同的 CLI。

```sh
git clone https://github.com/rzrleo/bili-video-agent.git
cd bili-video-agent
python -m pip install -e .
```

需要从没有字幕的视频转写口播或提取关键帧时，安装媒体依赖。其中包含 Faster Whisper 及 FFmpeg 的 Python 分发包。本地 Whisper 的模型权重在第一次转写时下载。

```sh
python -m pip install -e ".[media]"
```

## 搜索视频

```sh
bili-video search "想找用 pandas 清洗交易数据的实操视频" --keyword "pandas 金融 数据清洗 实战" --keyword "pandas 数据清洗 教程" --limit 8
```

命令查询 B 站站内视频索引，把多组结果按 BV 号去重并合并排序。支持 `--order relevance`、`latest` 和 `views`。自然语言描述由 Codex 拆成关键词并筛选候选。搜索结果附带来源、作者、简介、时长及匹配查询。

## 解析链接并提取总结材料

以下示例中的 `<BV号或视频链接>` 替换为实际输入。支持 `b23.tv` 分享短链、普通视频链接、BV/av 号和带 `?p=` 的分 P URL。可用 `--page` 覆盖分 P。

```sh
bili-video inspect "<BV号或视频链接>"
bili-video evidence "<BV号或视频链接>"
bili-video evidence "<BV号或视频链接>" --language zh --frames 6
```

`evidence` 优先读取平台字幕。无字幕时，下载所选分 P 的音频并在本机转写。选择 `--frames` 时另取视频轨道供 FFmpeg 抽帧，返回画面及其时间点。`--captions-only` 只尝试平台字幕，适合尽量减少开销。

输出目录默认是 `bili-results/`，每次任务创建独立子目录，包含 `evidence.json`、有转写时的 `transcript.txt` 和所请求的关键帧。临时音视频下载在任务结束时清理。默认最多转写 30 分钟、下载 180 MiB，可用 `--max-minutes` 和 `--max-media-mib` 调整。

公开搜索和视频详情可尝试匿名读取。某些字幕需要登录，可使用自有的本地 Netscape 格式 Cookie 文件，传路径即可。命令仅加载其中的 B 站 Cookie。

```sh
bili-video --cookies "<B站Cookie文件>" evidence "<BV号或视频链接>" --captions-only
```

## 在 Codex 中使用

在仓库内可让 Codex 读取 `skills/bili-video/SKILL.md`。要从其他项目使用，可把 `skills/bili-video/` 复制到 Codex 的用户技能目录 `~/.agents/skills/`，并保证 `bili-video` 命令在 PATH 中。之后可以直接用自然语言请求。

> 帮我在 B 站找三个讲 pandas 清洗交易数据的实操视频，给原始链接和各自适合谁。

> 帮我总结 `<链接>` 这个 B 站视频，标出关键步骤和时间点。

CLI 返回事实材料，Codex 负责筛选和写出总结。证据状态为 `ready`、`partial` 或 `unavailable`。`unavailable` 会保留诊断并返回退出码 3，命令或请求失败则返回非零退出码。

范围是可访问的 B 站普通投稿。前端页面和站内接口会变化，平台登录权限与地区可访问性仍然适用。

## 参考与许可证

客户端和 CLI 为本仓库独立实现。接口和工具行为参考 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)、[bilibili-cli](https://github.com/tamnd/bilibili-cli) 和 [yt-dlp 的 B 站提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)。音频转写使用 [Faster Whisper](https://github.com/SYSTRAN/faster-whisper)，画面抽取使用 [FFmpeg](https://ffmpeg.org/)。

本项目使用 [MIT 许可证](LICENSE)。
