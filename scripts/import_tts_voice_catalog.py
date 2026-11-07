#!/usr/bin/env python3
"""把厂商的系统音色灌成中台的「声音档案」，做成可按名字点选的音色表。

    python scripts/import_tts_voice_catalog.py --dry-run     # 只打印会写哪些档案
    python scripts/import_tts_voice_catalog.py               # 真的写进 registry
    python scripts/import_tts_voice_catalog.py --lang zh,en  # 只要中英文（默认）
    python scripts/import_tts_voice_catalog.py --lang all    # 全部语言

数据源：MiniMax `POST {MINIMAX_TTS_BASE_URL}/v1/get_voice`，body `{"voice_type":"all"}`。
这个调用**不花积分**（只是列音色）。2026-11-01 实测 303 条系统音色，其中
中文普通话 26、粤语 6、英语 6 —— **要靠它凑 200 条中英文是不够的**，
要么打开豆包渠道（几百条中文音色），要么自建克隆音色库（见 docs/api-docs.md §7.0）。

写档案走 `PUT {BASE}/v2/tts/voices/{voice_profile_id}`（要 SERVICE_TOKEN，
幂等：版本号自动递增，所以重复跑是安全的）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

MINIMAX_BASE = os.environ.get("MINIMAX_TTS_BASE_URL", "https://api.minimaxi.com").rstrip("/")
MINIMAX_KEY = os.environ.get("MINIMAX_TTS_API_KEY", "").strip()
BASE = os.environ.get("AI_CENTRE2_BASE_URL", "http://127.0.0.1:8320").rstrip("/")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "").strip()

# MiniMax 的 voice_id 前缀 → (语言代码, 语言 boost)
LANG_PREFIXES = [
    ("Chinese (Mandarin)_", ("zh-CN", "Chinese")),
    ("Chinese (Mandarin)", ("zh-CN", "Chinese")),
    ("Cantonese_", ("yue-HK", "Chinese")),
    ("English_", ("en-US", "English")),
    ("Japanese_", ("ja-JP", "Japanese")),
    ("Korean_", ("ko-KR", "Korean")),
    ("Spanish_", ("es-ES", "Spanish")),
    ("Portuguese_", ("pt-PT", "Portuguese")),
    ("French_", ("fr-FR", "French")),
    ("German_", ("de-DE", "German")),
    ("Russian_", ("ru-RU", "Russian")),
    ("Italian_", ("it-IT", "Italian")),
    ("Indonesian_", ("id-ID", "Indonesian")),
]


def detect(voice_id: str) -> tuple[str | None, str]:
    """按 voice_id 前缀判语言。判不出来就返回 (None, "auto") —— 猜成中文会谎报语言，
    宁可不写（档案里的 languages 留空，调用方自己指定 language）。"""
    for prefix, info in LANG_PREFIXES:
        if voice_id.startswith(prefix):
            return info
    return (None, "auto")


def slug(voice_id: str) -> str:
    s = voice_id.lower()
    s = s.replace("(", "").replace(")", "").replace("_", "-").replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    return "mm-" + re.sub(r"-{2,}", "-", s).strip("-")


def http_json(url: str, *, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def list_minimax_voices() -> list[dict]:
    d = http_json(f"{MINIMAX_BASE}/v1/get_voice",
                  method="POST", body={"voice_type": "all"},
                  headers={"Authorization": f"Bearer {MINIMAX_KEY}"})
    return d.get("system_voice") or []


def profile(voice: dict) -> dict:
    vid = voice["voice_id"]
    lang, boost = detect(vid)
    name = voice.get("voice_name") or vid
    return {
        "voice_profile_id": slug(vid),
        "display_name": f"{name}（{lang}）" if lang else name,
        "languages": [lang] if lang else [],
        "bindings": {"minimax": {"voice_id": vid, "language_boost": boost}},
        "fallback_order": ["minimax"],
        "version": 1,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="zh,en", help="逗号分隔的语言前缀：zh,en,yue,ja… 或 all")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not MINIMAX_KEY:
        print("缺 MINIMAX_TTS_API_KEY", file=sys.stderr)
        return 2
    if not args.dry_run and not SERVICE_TOKEN:
        print("缺 SERVICE_TOKEN（写 registry 要它）", file=sys.stderr)
        return 2

    wanted = None if args.lang.strip().lower() == "all" else set(args.lang.split(","))
    voices = list_minimax_voices()
    picked = []
    for v in voices:
        lang, _ = detect(v["voice_id"])
        if wanted is not None and (lang is None or lang.split("-")[0] not in wanted):
            continue
        picked.append(v)

    print(f"上游系统音色 {len(voices)} 条，按 --lang={args.lang} 选中 {len(picked)} 条")
    for v in picked:
        p = profile(v)
        if args.dry_run:
            print(f"  {p['voice_profile_id']:<52} {(p['languages'][0] if p['languages'] else '-'):<6} {v['voice_id']}")
            continue
        try:
            http_json(f"{BASE}/v2/tts/voices/{p['voice_profile_id']}", method="PUT", body=p,
                      headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
        except Exception as e:                       # 单条失败不打断整批
            print(f"  !! {p['voice_profile_id']}: {type(e).__name__}: {e}", file=sys.stderr)
    if not args.dry_run:
        print(f"写入完成：{len(picked)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
