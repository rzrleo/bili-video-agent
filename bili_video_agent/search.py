from __future__ import annotations

from .bilibili import BiliError, BilibiliClient


def search_videos(client: BilibiliClient, query: str, keywords: list[str], limit: int, order: str) -> dict:
    phrases = {}
    for phrase in [*keywords, query]:
        phrase = " ".join(phrase.split())
        if phrase:
            phrases.setdefault(phrase.casefold(), phrase)
    queries = list(phrases.values())
    if not queries or len(queries) > 5 or any(len(phrase) > 120 for phrase in queries):
        raise BiliError("请输入 1 至 5 个查询，每个查询最多 120 字。")

    matches: dict[str, dict] = {}
    scores: dict[str, float] = {}
    warnings = []
    successful = 0
    for phrase in queries:
        seen = set()
        rank = 0
        for page in range(1, 4):
            try:
                rows, pages = client.search_page(phrase, order, page)
            except BiliError as exc:
                warnings.append(f"查询 {phrase!r} 第 {page} 页失败。{exc}")
                break
            if page == 1:
                successful += 1
            for row in rows:
                video_id = row["bvid"]
                if video_id in seen:
                    continue
                seen.add(video_id)
                rank += 1
                candidate = matches.setdefault(video_id, {**row, "matched_queries": []})
                candidate["matched_queries"].append(phrase)
                scores[video_id] = scores.get(video_id, 0.0) + 1 / (30 + rank)
            if rank >= limit or page >= pages or not rows:
                break

    if not successful:
        raise BiliError("所有搜索查询均失败。" + " ".join(warnings))

    field = {"latest": "published_at", "views": "views"}.get(order)

    def ranking(item: dict) -> tuple[float, float]:
        score = scores[item["bvid"]]
        return (float(item.get(field) or 0), score) if field else (score, 0)

    results = sorted(matches.values(), key=ranking, reverse=True)[:limit]
    return {"query": query, "queries": queries, "order": order, "results": results, "warnings": warnings}
