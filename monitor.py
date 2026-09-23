"""
汎用 新着チェッカー（GitHub Actions 高頻度ループ版）

監視URLと通知先はすべて GitHub Secrets から読み込む。
ログには URL・商品名を一切出さない。

必要な Secrets:
  TARGET_URLS  … 監視するページのURL（複数ならカンマ区切り）
  WEBHOOK_URL  … 通知先のWebhook URL
"""

import json
import os
import random
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

# ================= 設定 =================
CHECK_INTERVAL_SEC = 5         # 待ち時間（秒）。読み込み込みで実際は約8秒おき
MAX_RUN_SEC = 5.5 * 3600       # 1回の実行の最大時間
QUIET_START_HOUR = 23          # 日本時間この時刻から停止
QUIET_END_HOUR = 8             # 日本時間この時刻まで停止
PERSIST_EVERY_SEC = 20 * 60    # 記録ファイルを保存する間隔
BROWSER_RESTART_EVERY = 300    # この回数ごとにブラウザ再起動
MAX_NOTIFY_PER_CHECK = 10

TARGET_URLS = [u.strip() for u in os.environ.get("TARGET_URLS", "").split(",") if u.strip()]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
SEEN_FILE = Path(__file__).parent / "seen.json"
JST = timezone(timedelta(hours=9))
# ========================================


def log(msg: str):
    print(f"[{datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def in_quiet_hours() -> bool:
    h = datetime.now(JST).hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            log("[WARN] 記録ファイルの読み込み失敗")
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def persist_to_repo():
    try:
        subprocess.run(["git", "add", "seen.json"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "update [skip ci]"],
                       check=False, capture_output=True)
        subprocess.run(["git", "push"], check=False, capture_output=True)
        log("記録を保存")
    except Exception:
        log("[WARN] 記録の保存に失敗")


def link_rule(url: str):
    """監視URLのパスから、商品リンクの判定ルールを自動で作る"""
    segments = [s for s in urlparse(url).path.split("/") if s]
    key = segments[-1] if segments else ""
    return f"a[href*='/{key}/']", set(segments)


def extract_items(page, url: str) -> list[dict]:
    selector, skip = link_rule(url)
    anchors = page.eval_on_selector_all(
        selector,
        """els => els.map(a => ({
            href: a.href,
            text: (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 200),
            img: (a.querySelector('img') || {}).src || ''
        }))""",
    )
    items, seen_hrefs = [], set()
    for a in anchors:
        href = a["href"].split("?")[0]
        last = href.rstrip("/").split("/")[-1]
        if href in seen_hrefs or not last or last in skip:
            continue
        if not any(c.isdigit() for c in last) and len(last) < 6:
            continue
        seen_hrefs.add(href)
        items.append({"id": last, "url": href,
                      "title": a["text"] or "(タイトル取得不可)", "img": a["img"]})
    return items


def load_page(page, url: str, first: bool):
    selector, _ = link_rule(url)
    if first:
        page.goto(url, wait_until="networkidle", timeout=60000)
    else:
        page.reload(wait_until="domcontentloaded", timeout=30000)
    page.wait_for_selector(selector, timeout=20000)
    page.wait_for_timeout(1200)
    page.mouse.wheel(0, 1500)
    page.wait_for_timeout(800)


def notify(item: dict):
    if not WEBHOOK_URL:
        log("[WARN] WEBHOOK_URL未設定")
        return
    embed = {
        "title": f"🆕 新着: {item['title'][:120]}",
        "url": item["url"],
        "color": 0x2ECC71,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if item["img"]:
        embed["image"] = {"url": item["img"]}
    try:
        r = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        elif r.status_code >= 400:
            log(f"[ERROR] 通知失敗（ステータス {r.status_code}）")
    except Exception as e:
        log(f"[ERROR] 通知エラー（{type(e).__name__}）")


def new_browser(p):
    browser = p.chromium.launch(headless=True)
    pages = [
        browser.new_page(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
        )
        for _ in TARGET_URLS
    ]
    return browser, pages


def main():
    if not TARGET_URLS:
        log("[ERROR] TARGET_URLS が未設定です")
        return
    if in_quiet_hours():
        log("停止時間帯なので終了")
        return

    seen = load_seen()
    first_run = len(seen) == 0
    start = last_persist = time.time()
    fail_streak = cycle = 0

    with sync_playwright() as p:
        browser, pages = new_browser(p)
        first_load = [True] * len(TARGET_URLS)

        while True:
            if time.time() - start > MAX_RUN_SEC:
                log("最大実行時間に到達。次の実行に引き継ぎ")
                break
            if in_quiet_hours():
                log("停止時間帯に入ったので終了")
                break

            cycle += 1
            if cycle % BROWSER_RESTART_EVERY == 0:
                browser.close()
                browser, pages = new_browser(p)
                first_load = [True] * len(TARGET_URLS)

            all_items, ok = [], False
            for i, url in enumerate(TARGET_URLS):
                try:
                    load_page(pages[i], url, first_load[i])
                    first_load[i] = False
                    got = extract_items(pages[i], url)
                    ok = ok or bool(got)
                    all_items.extend(got)
                except Exception as e:
                    log(f"[WARN] 取得失敗（{type(e).__name__}）")
                    first_load[i] = True

            if not ok:
                fail_streak += 1
                wait = min(CHECK_INTERVAL_SEC * (2 ** fail_streak), 300)
                log(f"[WARN] 取得0件（{fail_streak}回連続）。{wait:.0f}秒待機")
                time.sleep(wait)
                continue
            fail_streak = 0

            new_items = [it for it in all_items if it["id"] not in seen]
            if first_run:
                log(f"初回: {len(all_items)}件を登録（通知なし）")
                first_run = False
            elif new_items:
                log(f"新着 {len(new_items)}件を検知・通知")
                for it in new_items[:MAX_NOTIFY_PER_CHECK]:
                    notify(it)
                    time.sleep(0.5)
            elif cycle % 30 == 0:
                log(f"異常なし（{len(all_items)}件監視中・{cycle}回目）")

            seen.update(it["id"] for it in all_items)
            save_seen(seen)

            if time.time() - last_persist > PERSIST_EVERY_SEC:
                persist_to_repo()
                last_persist = time.time()

            time.sleep(max(1.0, CHECK_INTERVAL_SEC + random.uniform(-1.5, 1.5)))

        browser.close()

    save_seen(seen)
    persist_to_repo()


if __name__ == "__main__":
    main()
