from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx


ROOT = Path("/home/donxu/ai-centre")
RESULTS = ROOT / "runtime/validation/grsai-five-model-tvc-20260909/results.json"
OUTPUT = ROOT / "admin/public/grsai-five-model-tvc-assets"


def main() -> None:
    rows = json.loads(RESULTS.read_text(encoding="utf-8"))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for row in rows:
            url = row["result_urls"][0]
            response = client.get(url)
            response.raise_for_status()
            content = response.content
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(f"unexpected image format for {row['model']} {row['case']}")
            name = f"{row['model']}-{row['case']}.png"
            (OUTPUT / name).write_bytes(content)
            manifest.append({
                "model": row["model"],
                "case": row["case"],
                "file": name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "elapsed_seconds": row["elapsed_seconds"],
                "job_id": row["job_id"],
            })
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"archived {len(manifest)} images, {sum(x['bytes'] for x in manifest)} bytes")


if __name__ == "__main__":
    main()
