#!/usr/bin/env python3
"""libtv「畅用卡」守护：保证至少有一个有卡账号留在「用积分」。

为什么需要它：畅用卡开关是**账号级上游设置**（`rateLimitBenefitStatus`
0=畅用·慢队列免积分 / 1=用积分·快队列），控制台上单击即生效、无二次确认。
一旦把最后一个用积分号也切走，所有付费请求都会被塞进畅用慢队列
（实测 55~65 分钟出片，中台 30 分钟即判超时），表现就是「用积分生成都用不了」。

每 N 分钟由 systemd timer 跑一次：

  1. GET /video/benefits（经本机代理 127.0.0.1:8399，读上游原始状态）
  2. 统计「eligible（有畅用卡）且 status==1（用积分）」的账号数
  3. 为 0 时把指定账号切回用积分（POST .../benefit {enabled:false}），并回读校验
  4. 每次状态变化都写一行 audit jsonl；**并去 Caddy 访问日志里找同一时间窗内
     有没有对应的浏览器 POST —— 找不到就说明是上游自己改的**，这正是
     「明明没有人动它」这个现象的可证伪判据
  5. 变化与恢复都发飞书

只读 + 单点写入，不碰业务库、不碰 libtv-api 的代码。token 来自
`~/.config/credit-sync/env`（systemd EnvironmentFile 注入，权限 600）。

想临时放任「全部畅用」（例如专门跑慢队列攒素材），建一个空文件
`<data>/libtv_benefit_guard.pause` 即停手，删掉恢复。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))
ENV_FILE = os.path.expanduser("~/.config/credit-sync/env")


def load_env_file() -> None:
    """手动执行时 systemd 注入的变量不在，退回读同一个文件（不打印任何值）。"""
    if os.environ.get("LIBTV_PROXY_TOKEN") or not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


# 必须在下面这些常量之前跑：它们都是 import 时求值的
load_env_file()

PROXY = os.environ.get("LIBTV_PROXY_URL", "http://127.0.0.1:8399")
DATA_DIR = os.environ.get("CREDIT_SYNC_DATA_DIR") or "/home/donxu/ai-centre/runtime/jmapi-admin/data"
STATE = os.path.join(DATA_DIR, "libtv_benefit_guard.json")
AUDIT = os.path.join(DATA_DIR, "libtv_benefit_guard.jsonl")
PAUSE = os.path.join(DATA_DIR, "libtv_benefit_guard.pause")
CADDY_LOG = os.environ.get("CADDY_ACCESS_LOG") or "/home/donxu/ai-centre/runtime/caddy/logs/access.log"

WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "").strip()
TIMEOUT = float(os.environ.get("BENEFIT_GUARD_TIMEOUT", "30"))


# ────────────────────────────────────────────────────── 基础
def now_cn() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def audit(entry: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    entry = {"at": now_cn(), **entry}
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def api(method: str, path: str, body=None) -> dict:
    token = os.environ.get("LIBTV_PROXY_TOKEN", "").strip()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(PROXY + path, data=data, method=method)
    req.add_header("X-Libtv-Token", token)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {raw}") from None


def send_feishu(text: str) -> tuple[bool, str]:
    if not WEBHOOK:
        return False, "FEISHU_WEBHOOK 未配置"
    body = json.dumps({"msg_type": "text", "content": {"text": text}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WEBHOOK, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = r.read().decode("utf-8", "replace")
        try:
            d = json.loads(resp)
            return d.get("code", d.get("StatusCode")) in (0, None), resp[:200]
        except ValueError:
            return False, resp[:200]
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ───────────────────────────────────────────── 谁动的：Caddy 访问日志
def read_tail(path: str, max_bytes: int = 4_000_000) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()          # 丢掉被切半的第一行
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def browser_benefit_posts(since_ts: float) -> dict[str, list[dict]]:
    """返回 {账号名: [{ts, ip}]}，只统计窗口内的 POST .../benefit。"""
    hits: dict[str, list[dict]] = {}
    for line in read_tail(CADDY_LOG):
        if "/benefit" not in line or '"POST"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (rec.get("ts") or 0) < since_ts:
            continue
        req = rec.get("request") or {}
        uri = (req.get("uri") or "").split("?")[0]
        if req.get("method") != "POST" or not uri.endswith("/benefit"):
            continue
        parts = uri.rstrip("/").split("/")
        if len(parts) < 2:
            continue
        name = urllib.parse.unquote(parts[-2])
        hits.setdefault(name, []).append({"ts": rec.get("ts"), "ip": req.get("client_ip")})
    return hits


def classify_change(name: str, hits: dict[str, list[dict]]) -> tuple[str, str]:
    got = hits.get(name) or []
    if not got:
        return "upstream_or_unknown", "时间窗内 Caddy 无对应的浏览器 POST"
    ips = sorted({h["ip"] for h in got if h.get("ip")})
    return "browser", "浏览器点击，来自 " + (",".join(ips) or "未知 IP")


# ────────────────────────────────────────────────────── 主流程
def pick_target(state: dict, eligible: dict[str, dict]) -> str | None:
    """优先恢复「上一次还是用积分」的那个号，否则挑余额最大的有卡号。"""
    last = state.get("last_paid")
    if last and last in eligible and eligible[last].get("status") == 0:
        return last
    cands = [n for n, a in eligible.items() if a.get("status") == 0]
    if not cands:
        return None
    scores: dict[str, float] = {}
    try:
        for a in (api("GET", "/video/balances").get("accounts") or []):
            if a.get("name"):
                scores[a["name"]] = a.get("balance") or 0
    except Exception:  # noqa: BLE001
        pass
    return max(cands, key=lambda n: scores.get(n, 0))


def should_alert(state: dict, now: float) -> bool:
    times = [t for t in (state.get("restore_times") or []) if now - t < 3600]
    if len(times) < 4:
        return True
    return now - (state.get("last_alert_at") or 0) >= 3600


def main() -> int:
    now = time.time()
    state = load_state()
    paused = os.path.exists(PAUSE)
    alerts: list[str] = []

    try:
        resp = api("GET", "/video/benefits")
    except Exception as e:  # noqa: BLE001
        print(f"[{now_cn()}] 读 benefits 失败，本轮不动: {e}", flush=True)
        return 0

    accts = resp.get("accounts") or []
    eligible = {
        a["name"]: a for a in accts
        if a.get("name") and a.get("eligible") is True and a.get("status") is not None
    }
    paid = [n for n, a in eligible.items() if a.get("status") == 1]

    # ── 与上一轮比对，记录变化 ────────────────────────────
    prev = state.get("accounts") or {}
    cur = {n: {"status": a.get("status"), "daysLeft": a.get("daysLeft")}
           for n, a in eligible.items()}
    changed = [(n, prev[n].get("status"), cur[n]["status"])
               for n in cur if n in prev and prev[n].get("status") != cur[n]["status"]]
    dropped = [n for n in prev if n not in cur]

    if state and (changed or dropped):
        since = float(state.get("checked_epoch") or (now - 600))
        hits = browser_benefit_posts(since)
        for name, before, after in changed:
            src, why = classify_change(name, hits)
            audit({"event": "status_change", "account": name,
                   "before": before, "after": after, "source": src, "why": why})
        for name in dropped:
            audit({"event": "dropped_from_eligible", "account": name,
                   "before": prev[name].get("status")})

    # ── 守护：有卡账号全在畅用 → 拉一个回来 ────────────────
    restored = None
    if eligible and not paid:
        if paused:
            audit({"event": "guard_paused", "eligible": sorted(eligible)})
            print(f"[{now_cn()}] 有卡账号全在畅用，但守护已暂停（{PAUSE}）", flush=True)
        else:
            target = pick_target(state, eligible)
            if not target:
                alerts.append("🚨 libtv 没有任何账号在用积分，且找不到可恢复的有卡账号\n"
                              f"时间：{now_cn()}\n有卡账号：{', '.join(sorted(eligible)) or '无'}")
                audit({"event": "guard_failed", "reason": "no_candidate"})
            else:
                src, why = classify_change(target, browser_benefit_posts(now - 900))
                try:
                    api("POST", f"/video/account/{urllib.parse.quote(target, safe='')}/benefit",
                        {"enabled": False})
                    after = {a.get("name"): a for a in (api("GET", "/video/benefits").get("accounts") or [])}
                    ok = (after.get(target) or {}).get("status") == 1
                except Exception as e:  # noqa: BLE001
                    ok = False
                    why = f"{why}；恢复调用失败：{e}"

                audit({"event": "restore", "account": target, "ok": ok,
                       "flipped_by": src, "why": why,
                       "eligible_state": {n: a.get("status") for n, a in eligible.items()}})

                state["restore_times"] = ([t for t in (state.get("restore_times") or [])
                                           if now - t < 3600] + [now])
                if should_alert(state, now):
                    msg = [f"{'✅ 已自动恢复' if ok else '❌ 恢复失败'}：libtv 用积分号被清空",
                           f"账号：{target}",
                           f"恢复前：{len(eligible)} 个有卡账号全部在畅用",
                           f"原因判定：{why}",
                           f"时间：{now_cn()}",
                           f"近 1 小时已恢复 {len(state['restore_times'])} 次"]
                    alerts.append("\n".join(msg))
                restored = target if ok else None
                if ok:
                    paid = [target]
                    cur[target] = {"status": 1, "daysLeft": (eligible.get(target) or {}).get("daysLeft")}

    if not eligible:
        audit({"event": "no_eligible_account"})

    # ── 记住最后一个用积分号，供下次恢复时定向 ────────────
    if paid:
        state["last_paid"] = paid[0]
        state["last_paid_at"] = now_cn()

    state.update({
        "checked_at": now_cn(),
        "checked_epoch": now,
        "accounts": cur,
        "paused": paused,
    })

    for text in alerts:
        ok, r = send_feishu(text)
        state["last_alert_at"] = now
        state["last_alert"] = {"at": now_cn(), "sent": ok, "resp": r, "text": text}
        print(f"[{now_cn()}] alert sent={ok} {r}", flush=True)

    save_state(state)

    summary = " ".join(f"{n}={cur[n]['status']}" for n in sorted(cur)) or "无有卡账号"
    print(f"[{now_cn()}] paid={paid or '无'} {summary} "
          f"changes={len(changed)} dropped={len(dropped)} restored={restored}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
