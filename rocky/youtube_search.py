"""Isolated, time-bounded YouTube metadata lookup; never downloads media."""
import json
import sys


def search(query):
    from yt_dlp import YoutubeDL
    with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True,
                    "skip_download": True, "socket_timeout": 10, "retries": 1,
                    "extractor_retries": 1, "cachedir": False}) as youtube:
        result = youtube.extract_info("ytsearch1:" + query, download=False)
    entries = result.get("entries", []) if result else []
    entry = next((item for item in entries if item), None)
    if not entry:
        raise ValueError("No YouTube results")
    return {"id": entry["id"], "title": entry.get("title", query)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(search(sys.argv[1]), ensure_ascii=False))
