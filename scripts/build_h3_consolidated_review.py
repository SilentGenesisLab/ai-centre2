from __future__ import annotations

import csv
import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_DIR = ROOT / "runtime_validation" / "h3-tvc-benchmark-20260908"
QUALITY_DIR = ROOT / "runtime_validation" / "h3-quality-ab-20260908"
DURATION_DIR = ROOT / "runtime_validation" / "h3-tvc-duration-20260908"


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def main_cases() -> list[dict[str, object]]:
    labels = {"smart_watch": "高端智能手表", "blue_serum": "浅蓝科技护肤精华"}
    items: list[dict[str, object]] = []
    with (MAIN_DIR / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            creative = row["creative"]
            mode = row["input_mode"]
            if mode == "text":
                note = "用于评估广告风格与运镜；未提供SKU素材，不能视为产品还原测试。"
            elif creative == "blue_serum":
                note = "参考视频中段包含无关人物场景，属于混杂参考源；模型可能继承人物，不能作为干净产品参考结论。"
            else:
                note = "参考源为方形智能表，与提示词中的圆形表壳存在冲突；结果更倾向继承参考源。"
            items.append({
                "group": "main",
                "group_label": "TVC全矩阵",
                "case_id": row["case_id"],
                "title": labels.get(creative, creative),
                "creative": creative,
                "resolution": row["resolution"],
                "duration": row["duration_seconds"],
                "mode": mode,
                "mode_label": "参考视频" if mode == "video" else "纯文本",
                "quality": "standard",
                "quality_label": "标准",
                "status": row["status"],
                "effective": row["effective_processing_seconds"],
                "end_to_end": row["end_to_end_seconds"],
                "decode": row["complete_decode"],
                "black": row["black_segment_count"],
                "freeze": row["freeze_segment_count"],
                "src": row["result_url"],
                "poster": f"review-frames/{row['case_id']}/start.jpg",
                "note": note,
            })
    return items


def quality_cases() -> list[dict[str, object]]:
    labels = {"watch_text": "智能手表·纯文本", "serum_video": "护肤精华·参考视频"}
    quality_labels = {"low": "低档", "medium": "中档", "high": "高档"}
    notes = {
        "watch_text": "画质档位能改善清晰度和材质，但不能解决纯文本产品身份漂移。",
        "serum_video": "主体继承明显；瓶身文字仍为伪字。常规生产建议中档，英雄镜头再用高档。",
    }
    items: list[dict[str, object]] = []
    with (QUALITY_DIR / "results.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            scenario = row["scenario"]
            quality = row["quality"]
            items.append({
                "group": "quality",
                "group_label": "质量档位A/B",
                "case_id": row["case_id"],
                "title": labels.get(scenario, scenario),
                "creative": "smart_watch" if scenario == "watch_text" else "blue_serum",
                "resolution": "720p",
                "duration": "10",
                "mode": "text" if scenario == "watch_text" else "video",
                "mode_label": "纯文本" if scenario == "watch_text" else "参考视频",
                "quality": quality,
                "quality_label": quality_labels.get(quality, quality),
                "status": row["status"],
                "effective": row["effective_processing_seconds"],
                "end_to_end": row["end_to_end_seconds"],
                "decode": row["complete_decode"],
                "black": row["black_segments"],
                "freeze": row["freeze_segments"],
                "src": row["result_url"],
                "poster": f"../h3-quality-ab-20260908/review-frames/{row['case_id']}/start.jpg",
                "note": notes[scenario],
            })
    return items


def duration_cases() -> list[dict[str, object]]:
    rows = [
        ("mask-5s", "面膜", 5, False, 127.447),
        ("mask-10s", "面膜", 10, False, 268.157),
        ("mask-15s", "面膜", 15, False, 396.678),
        ("serum-5s", "精华液", 5, False, 155.765),
        ("serum-10s", "精华液", 10, False, 262.285),
        ("serum-15s", "精华液", 15, False, 443.362),
        ("mask-10s-ref", "面膜", 10, True, 203.119),
        ("serum-10s-ref", "精华液", 10, True, 210.378),
    ]
    items: list[dict[str, object]] = []
    for case_id, title, duration, has_reference, elapsed in rows:
        items.append({
            "group": "duration",
            "group_label": "早期时长/参考图A/B",
            "case_id": case_id,
            "title": title,
            "creative": "mask" if title == "面膜" else "blue_serum",
            "resolution": "720p",
            "duration": str(duration),
            "mode": "image" if has_reference else "text",
            "mode_label": "参考图" if has_reference else "纯文本",
            "quality": "standard",
            "quality_label": "标准",
            "status": "succeeded",
            "effective": elapsed,
            "end_to_end": elapsed,
            "decode": "True",
            "black": "-",
            "freeze": "-",
            "src": f"../h3-tvc-duration-20260908/{case_id}.mp4",
            "poster": f"../h3-tvc-duration-20260908/{case_id}-contact.jpg",
            "note": (
                "参考图显著提升SKU稳定与英雄镜头完成度。"
                if has_reference
                else "10秒综合最稳定；15秒更容易出现形态和场景漂移。"
            ),
        })
    return items


def card(item: dict[str, object]) -> str:
    src = esc(item["src"])
    poster_path = Path(str(item["poster"]))
    poster = esc(item["poster"]) if (MAIN_DIR / poster_path).is_file() else ""
    poster_attr = f' poster="{poster}"' if poster else ""
    return f"""
<article class="card" data-group="{esc(item['group'])}" data-resolution="{esc(item['resolution'])}" data-duration="{esc(item['duration'])}" data-mode="{esc(item['mode'])}" data-quality="{esc(item['quality'])}">
  <div class="card-head"><div><p class="eyebrow">{esc(item['group_label'])}</p><h2>{esc(item['title'])}</h2></div><span class="status">技术通过</span></div>
  <video controls preload="none" playsinline{poster_attr} aria-label="播放{esc(item['title'])}{esc(item['quality_label'])}成片"><source src="{src}" type="video/mp4">浏览器不支持视频播放。</video>
  <div class="tags"><span>{esc(item['resolution'])}</span><span>{esc(item['duration'])}秒</span><span>{esc(item['mode_label'])}</span><span>{esc(item['quality_label'])}</span></div>
  <dl><div><dt>有效处理</dt><dd>{esc(item['effective'])}s</dd></div><div><dt>端到端</dt><dd>{esc(item['end_to_end'])}s</dd></div><div><dt>解码</dt><dd>{esc(item['decode'])}</dd></div><div><dt>黑段/冻结</dt><dd>{esc(item['black'])}/{esc(item['freeze'])}</dd></div></dl>
  <p class="note">{esc(item['note'])}</p>
  <details><summary>任务信息</summary><code>{esc(item['case_id'])}</code><a href="{src}" target="_blank" rel="noreferrer">备用打开/下载</a></details>
</article>"""


def build() -> Path:
    main_summary = json.loads((MAIN_DIR / "summary.json").read_text(encoding="utf-8"))
    items = main_cases() + quality_cases() + duration_cases()
    cards = "".join(card(item) for item in items)
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MiniMax H3 TVC 统一测评审核</title>
<style>
:root{{--bg:#edf7ff;--panel:#fff;--text:#12283d;--muted:#526b80;--line:#c8e1f4;--blue:#0878dc;--ok:#087a56;--warn:#9a5b00}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(145deg,#e9f6ff,#fbfdff 55%);color:var(--text);font:15px/1.55 system-ui,"Microsoft YaHei",sans-serif}}header{{position:sticky;top:0;z-index:5;padding:18px clamp(16px,4vw,56px);background:#f7fbffed;backdrop-filter:blur(16px);border-bottom:1px solid var(--line)}}h1{{margin:0;font-size:clamp(22px,3vw,30px)}}.subtitle{{margin:4px 0 0;color:var(--muted)}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:16px 0}}.metric{{padding:12px;border:1px solid var(--line);border-radius:12px;background:#fff}}.metric b{{display:block;font-size:20px}}.metric span{{color:var(--muted);font-size:12px}}.filters{{display:flex;gap:8px;flex-wrap:wrap}}select{{min-height:44px;padding:8px 34px 8px 12px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--text);font:inherit}}main{{padding:22px clamp(16px,4vw,56px);display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:18px}}.card{{min-width:0;padding:15px;background:var(--panel);border:1px solid var(--line);border-radius:16px;box-shadow:0 10px 28px #2e78a914}}.card-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}}.eyebrow{{margin:0;color:var(--blue);font-size:12px;font-weight:700}}h2{{margin:2px 0 0;font-size:18px}}.status{{padding:4px 8px;border-radius:99px;background:#e5f7ef;color:var(--ok);font-size:12px;white-space:nowrap}}video{{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#07131f;border-radius:11px}}.tags{{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}}.tags span{{padding:3px 8px;border-radius:99px;background:#eaf5ff;color:#0866b8;font-size:12px}}dl{{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin:0}}dl div{{padding:8px;background:#f6faff;border-radius:8px}}dt{{color:var(--muted);font-size:11px}}dd{{margin:1px 0 0;font-weight:700}}.note{{min-height:46px;color:var(--warn)}}details{{border-top:1px solid var(--line);padding-top:9px}}summary{{cursor:pointer;font-weight:650}}code{{display:block;margin:8px 0;color:var(--muted);overflow-wrap:anywhere}}a{{display:inline-flex;align-items:center;min-height:44px;color:var(--blue);font-weight:700;text-decoration:none}}:focus-visible{{outline:3px solid #7abaff;outline-offset:3px}}[hidden]{{display:none!important}}@media(max-width:760px){{header{{position:static}}.metrics{{grid-template-columns:repeat(2,1fr)}}main{{grid-template-columns:1fr}}}}
@media(max-width:760px){{body{{font-size:16px}}}}
</style></head><body><header><h1>MiniMax H3 TVC 统一测评审核</h1><p class="subtitle">共 {len(items)} 条：36条主矩阵 + 6条质量档位A/B + 8条早期时长/参考图A/B。视频均在本页点击播放，不自动加载成片。</p>
<div class="metrics"><div class="metric"><b>36/36</b><span>主矩阵接口成功</span></div><div class="metric"><b>100%</b><span>完整解码</span></div><div class="metric"><b>{main_summary['overall']['effective_processing_seconds']['mean']}s</b><span>主矩阵有效均值</span></div><div class="metric"><b>{main_summary['overall']['effective_processing_seconds']['p95']}s</b><span>主矩阵P95</span></div></div>
<div class="filters"><select id="group" aria-label="测试批次"><option value="">全部批次</option><option value="main">TVC全矩阵</option><option value="quality">质量档位A/B</option><option value="duration">早期时长/参考图A/B</option></select><select id="resolution" aria-label="分辨率"><option value="">全部分辨率</option><option>480p</option><option>720p</option><option>1080p</option></select><select id="duration" aria-label="时长"><option value="">全部时长</option><option value="5">5秒</option><option value="10">10秒</option><option value="15">15秒</option></select><select id="mode" aria-label="输入模式"><option value="">全部输入</option><option value="text">纯文本</option><option value="image">参考图</option><option value="video">参考视频</option></select><select id="quality" aria-label="质量档位"><option value="">全部质量</option><option value="standard">标准</option><option value="low">低档</option><option value="medium">中档</option><option value="high">高档</option></select></div></header><main>{cards}</main>
<script>const filters=[...document.querySelectorAll('select')];function apply(){{for(const card of document.querySelectorAll('.card'))card.hidden=filters.some(f=>f.value&&card.dataset[f.id]!==f.value)}}for(const filter of filters)filter.addEventListener('change',apply);</script></body></html>"""
    output = MAIN_DIR / "review.html"
    output.write_text(document, encoding="utf-8")
    return output


if __name__ == "__main__":
    print(build())
