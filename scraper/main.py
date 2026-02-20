import os
import re
import json
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

EVENT_LIST_URL = "https://o-lens.com/event/list"
STATE_PATH = Path("state/olens_seen.json")

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

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

def scrape_event_links() -> list[str]:
    links: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(EVENT_LIST_URL, wait_until="networkidle", timeout=60000)

        anchors = page.locator('a[href*="/event/view/"]').all()
        for a in anchors:
            href = a.get_attribute("href") or ""
            if not href:
                continue
            if href.startswith("/"):
                href = "https://o-lens.com" + href

            if re.search(r"/event/view/\d+", href):
                links.append(href)

        browser.close()

    # 중복 제거(순서 유지)
    dedup = []
    s = set()
    for u in links:
        if u not in s:
            dedup.append(u)
            s.add(u)
    return dedup

def main():
    seen = load_seen()
    links = scrape_event_links()

    new_links = [u for u in links if u not in seen]

    print("links:", len(links))
    print("seen:", len(seen))
    print("new_links:", len(new_links))

    # 첫 실행 도배 방지(원하시면 활성화)
    # if not seen and links:
    #     new_links = []

    if new_links:
        for url in new_links[:10]:
            post_slack(f"[O-Lens 신규 이벤트] {url}")
            seen.add(url)
        save_seen(seen)

if __name__ == "__main__":
    main()
