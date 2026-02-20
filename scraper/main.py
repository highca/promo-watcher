import os
import re
import json
import time
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

EVENT_LIST_URL = "https://o-lens.com/event/list"
STATE_PATH = Path("state/olens_seen.json")

# GitHub Actions Secrets에서 주입
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

# Headless 차단/렌더링 이슈 완화를 위한 UA
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

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

def scrape_event_links(max_retries: int = 2) -> list[str]:
    """
    O-Lens 이벤트 목록 페이지는 SPA/JS 렌더링 기반이라 networkidle 대기가 무한정 늘어질 수 있습니다.
    따라서 domcontentloaded로 빠르게 로딩 후, selector 등장/대기 방식으로 안정화합니다.
    """
    print("[scrape] start")
    last_err = None

    for attempt in range(1, max_retries + 1):
        print(f"[scrape] attempt {attempt}/{max_retries}")
        try:
            with sync_playwright() as p:
                print("[scrape] launching browser")
                browser = p.chromium.launch(headless=True)

                page = browser.new_page(user_agent=USER_AGENT)
                page.set_default_timeout(30000)

                print("[scrape] goto list page (domcontentloaded)")
                page.goto(EVENT_LIST_URL, wait_until="domcontentloaded", timeout=60000)

                # JS 렌더링 대기 (너무 길게 잡지 않음)
                print("[scrape] wait for render")
                try:
                    page.wait_for_selector('a[href*="/event/view/"]', timeout=20000)
                except PlaywrightTimeoutError:
                    # selector가 늦게 뜨거나 아예 안 뜰 수 있어 fallback
                    print("[scrape] selector wait timeout - fallback to fixed sleep")
                    page.wait_for_timeout(6000)

                print("[scrape] collect anchors")
                anchors = page.locator('a[href*="/event/view/"]').all()
                print("[scrape] anchors:", len(anchors))

                links: list[str] = []
                for a in anchors:
                    href = a.get_attribute("href") or ""
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://o-lens.com" + href
                    if re.search(r"/event/view/\d+", href):
                        links.append(href)

                print("[scrape] closing browser")
                browser.close()

                links = _dedup_keep_order(links)
                print("[scrape] done, links:", len(links))
                return links

        except Exception as e:
            last_err = e
            print(f"[scrape] error on attempt {attempt}: {repr(e)}")
            # 짧은 backoff
            time.sleep(2 * attempt)

    # 재시도 후에도 실패하면 예외로 처리(워크플로 빨간불로 원인 추적)
    raise RuntimeError(f"Failed to scrape after {max_retries} attempts: {repr(last_err)}")

def main():
    print("[main] start")
    seen = load_seen()
    print("[main] seen:", len(seen))

    links = scrape_event_links(max_retries=2)
    print("[main] links:", len(links))

    new_links = [u for u in links if u not in seen]
    print("[main] new_links:", len(new_links))

    # 신규가 있을 때만 Slack 전송
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
