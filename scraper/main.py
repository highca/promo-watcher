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

def _stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

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

def _dedup_keep_order(items: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for it in items:
        key = it.get("url") or it.get("title") or it.get("image")
        if not key:
            continue
        if key not in seen:
            out.append(it)
            seen.add(key)
    return out

def scrape_events_click_through(max_items: int = 20, max_retries: int = 2) -> list[dict]:
    """
    O-Lens 이벤트 리스트는 <a href>가 아니라 div(role=button) 카드 클릭으로 라우팅됩니다.
    따라서 카드 클릭 -> /event/view/... 이동한 URL을 수집합니다.
    (업로드된 HTML에서도 board-information__wrapper 카드 구조가 확인됩니다.)   [oai_citation:1‡olens_list_20260220_015531_attempt1.html](sediment://file_000000002a6872088d5ee46ffcf7a726)
    """
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
                    viewport={"width": 750, "height": 1500},
                )
                page = context.new_page()
                page.set_default_timeout(30000)

                print("[scrape] goto list")
                resp = page.goto(EVENT_LIST_URL, wait_until="domcontentloaded", timeout=60000)
                print("[scrape] status:", resp.status if resp else None)
                print("[scrape] url:", page.url)

                # 카드가 나타날 때까지 대기
                cards = page.locator("div.board-information__wrapper")
                try:
                    cards.first.wait_for(timeout=25000)
                except PlaywrightTimeoutError:
                    # 진단 저장
                    shot = DEBUG_DIR / f"list_timeout_{stamp}.png"
                    html = DEBUG_DIR / f"list_timeout_{stamp}.html"
                    page.screenshot(path=str(shot), full_page=True)
                    html.write_text(page.content(), encoding="utf-8")
                    raise RuntimeError("Event cards not found on list page (timeout).")

                total = cards.count()
                print("[scrape] cards:", total)
                n = min(total, max_items)

                results: list[dict] = []

                for i in range(n):
                    # 리스트 화면에서 카드 정보(제목/구분/이미지) 먼저 추출
                    card = cards.nth(i)

                    chip = ""
                    title = ""
                    image = ""

                    try:
                        chip = (card.locator(".board-information__chip .v-chip__content").first.inner_text() or "").strip()
                    except Exception:
                        chip = ""

                    try:
                        title = (card.locator(".board-information__title").first.inner_text() or "").strip()
                    except Exception:
                        title = ""

                    try:
                        image = card.locator("img.board-information__image").first.get_attribute("src") or ""
                    except Exception:
                        image = ""

                    print(f"[scrape] #{i+1}/{n} title:", title)

                    # 클릭 -> 상세로 이동 -> URL 수집
                    url = ""
                    try:
                        card.click(timeout=10000)
                        page.wait_for_url(re.compile(r".*/event/view/.*"), timeout=25000)
                        url = page.url
                    except PlaywrightTimeoutError:
                        # 클릭했는데 상세로 안 가는 경우(라우팅 실패/차단) 진단 저장 후 다음 카드로
                        fail_shot = DEBUG_DIR / f"detail_timeout_{stamp}_idx{i+1}.png"
                        page.screenshot(path=str(fail_shot), full_page=True)
                        print(f"[scrape] detail timeout on idx {i+1}, continue")
                    finally:
                        # 다시 리스트로 복귀 (url이 상세로 바뀌었으면 뒤로가기)
                        if "/event/view/" in page.url:
                            try:
                                page.go_back(wait_until="domcontentloaded", timeout=30000)
                                # 리스트 카드 다시 로드 확인
                                page.locator("div.board-information__wrapper").first.wait_for(timeout=20000)
                                cards = page.locator("div.board-information__wrapper")
                            except Exception:
                                # 복귀 실패 시 리스트 재접속
                                page.goto(EVENT_LIST_URL, wait_until="domcontentloaded", timeout=60000)
                                page.locator("div.board-information__wrapper").first.wait_for(timeout=25000)
                                cards = page.locator("div.board-information__wrapper")

                    if url:
                        results.append(
                            {
                                "url": url,
                                "title": title,
                                "chip": chip,
                                "image": image,
                            }
                        )

                # 진단용: 리스트 화면 스냅샷 1장 저장
                try:
                    list_shot = DEBUG_DIR / f"olens_list_{stamp}.png"
                    page.screenshot(path=str(list_shot), full_page=True)
                    print("[scrape] saved list screenshot:", str(list_shot))
                except Exception:
                    pass

                context.close()
                browser.close()

                results = _dedup_keep_order(results)
                print("[scrape] results:", len(results))
                return results

        except Exception as e:
            last_err = e
            print("[scrape] error:", repr(e))
            time.sleep(2 * attempt)

    raise RuntimeError(f"Failed to scrape after retries: {repr(last_err)}")

def main():
    print("[main] start")
    seen = load_seen()
    print("[main] seen:", len(seen))

    events = scrape_events_click_through(max_items=20, max_retries=2)
    print("[main] events:", len(events))

    new_events = [e for e in events if e.get("url") and e["url"] not in seen]
    print("[main] new_events:", len(new_events))

    if new_events:
        for e in new_events[:10]:
            msg = f"[O-Lens 신규 이벤트] {e.get('title','').strip()}\n{e['url']}"
            # 원하시면 이미지도 같이 보고 싶을 때는 아래 1줄을 추가로 붙이셔도 됩니다.
            # if e.get("image"): msg += f"\n{e['image']}"
            post_slack(msg)
            seen.add(e["url"])
        save_seen(seen)
        print("[main] state saved")
    else:
        print("[main] no new events, exit")

if __name__ == "__main__":
    main()
