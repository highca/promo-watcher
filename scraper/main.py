import os
import re
import json
import time
import requests
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

EVENT_LIST_URL = "https://o-lens.com/event/list"
STATE_PATH = Path("state/olens_seen.json")

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

def post_slack(text: str):
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    resp.raise_for_status()

def load_seen() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()

def save_seen(seen: set[str]):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def _dedup_keep_order(urls: list[str]) -> list[str]:
    out = []
    s = set()
    for u in urls:
        if u not in s:
            out.append(u)
            s.add(u)
    return out

def _stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def scrape_event_links(max_retries: int = 2) -> list[str]:
    print("[scrape] start")
    last_err = None

    for attempt in range(1, max_retries + 1):
        stamp = _stamp()
        print(f"[scrape] attempt {attempt}/{max_retries}")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )

                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                    viewport={"width": 1365, "height": 900},
                )
                page = context.new_page()
                page.set_default_timeout(30000)

                print("[scrape] goto list page (domcontentloaded)")
                resp = page.goto(EVENT_LIST_URL, wait_until="domcontentloaded", timeout=60000)

                # 진단 로그
                status = resp.status if resp else None
                print("[scrape] goto status:", status)
                print("[scrape] final url:", page.url)
                print("[scrape] title:", page.title())

                # 네트워크가 계속 도는 SPA를 대비: networkidle은 짧게만 시도
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                    print("[scrape] networkidle reached")
                except PlaywrightTimeoutError:
                    print("[scrape] networkidle timeout (ok)")

                # 이벤트 링크가 등장하는지 대기
                print("[scrape] wait for selector")
                try:
                    page.wait_for_selector('a[href*="/event/view/"]', timeout=20000)
                    print("[scrape] selector appeared")
                except PlaywrightTimeoutError:
                    print("[scrape] selector wait timeout - save debug files")

                # 진단 파일 저장: 스크린샷 + HTML
                shot_path = DEBUG_DIR / f"olens_list_{stamp}_attempt{attempt}.png"
                html_path = DEBUG_DIR / f"olens_list_{stamp}_attempt{attempt}.html"
                try:
                    page.screenshot(path=str(shot_path), full_page=True)
                    html = page.content()
                    html_path.write_text(html, encoding="utf-8")
                    print("[scrape] saved:", str(shot_path), str(html_path))
                except Exception as e:
                    print("[scrape] debug save failed:", repr(e))

                # 링크 수집 (셀렉터를 넓게 시도)
                print("[scrape] collect anchors")
                anchors = page.locator('a[href*="/event/"]').all()
                print("[scrape] anchors(total /event/):", len(anchors))

                links: list[str] = []
                for a in anchors:
                    href = a.get_attribute("href") or ""
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://o-lens.com" + href
                    if re.search(r"/event/view/\d+", href):
                        links.append(href)

                links = _dedup_keep_order(links)
                print("[scrape] links(found view/*):", len(links))

                context.close()
                browser.close()

                return links

        except Exception as e:
            last_err = e
            print(f"[scrape] error on attempt {attempt}: {repr(e)}")
            time.sleep(2 * attempt)

    raise RuntimeError(f"Failed to scrape after {max_retries} attempts: {repr(last_err)}")

def main():
    print("[main] start")
    seen = load_seen()
    print("[main] seen:", len(seen))

    links = scrape_event_links(max_retries=2)
    print("[main] links:", len(links))

    new_links = [u for u in links if u not in seen]
    print("[main] new_links:", len(new_links))

    if new_links:
        for url in new_links[:10]:
            post_slack(f"[O-Lens 신규 이벤트] {url}")
            seen.add(url)
        save_seen(seen)
        print("[main] state saved")
    else:
        print("[main] no new events, exit")

if __name__ == "__main__":
    main()
