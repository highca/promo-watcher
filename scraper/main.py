import os
import re
import json
import time
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

STATE_PATH = Path("state/seen.json")
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

INIT_SILENT = True

DEFAULT_MAX_ITEMS = 30
SITE_TIMEOUT_SEC = 35   # 사이트 하나당 최대 처리 시간(초)


def _stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def post_slack(text: str):
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    resp.raise_for_status()


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _dedup_keep_order(items: list[dict], key_field: str = "key") -> list[dict]:
    out = []
    s = set()
    for it in items:
        k = it.get(key_field) or it.get("url") or it.get("title")
        if not k:
            continue
        if k not in s:
            out.append(it)
            s.add(k)
    return out


def _new_context(browser):
    return browser.new_context(
        user_agent=USER_AGENT,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={"width": 750, "height": 1500},
    )


def _save_debug(page, prefix: str):
    stamp = _stamp()
    shot = DEBUG_DIR / f"{prefix}_{stamp}.png"
    html = DEBUG_DIR / f"{prefix}_{stamp}.html"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception:
        pass
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    print("[debug] saved", str(shot), str(html))


def _abs_url(base: str, href: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("javascript:"):
        return ""
    return urljoin(base, href)


def _same_host(url_a: str, url_b: str) -> bool:
    try:
        return urlparse(url_a).netloc == urlparse(url_b).netloc
    except Exception:
        return False


# -----------------------------
# 공용 함수
# -----------------------------

def safe_goto(page, url: str, label: str = ""):
    print(f"[goto]{'['+label+']' if label else ''} {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1200)


def scrape_list_page_anchors(page, list_url: str, include_patterns: list[str],
                            exclude_patterns: list[str] | None = None,
                            max_items: int = DEFAULT_MAX_ITEMS) -> list[dict]:
    exclude_patterns = exclude_patterns or []
    safe_goto(page, list_url, "list")

    anchors = page.locator("a[href]").all()
    base = page.url
    results = []

    for a in anchors:
        href = a.get_attribute("href") or ""
        url = _abs_url(base, href)
        if not url:
            continue

        if include_patterns and not any(re.search(p, url) for p in include_patterns):
            continue
        if exclude_patterns and any(re.search(p, url) for p in exclude_patterns):
            continue

        title = (a.inner_text() or "").strip()
        if not title:
            try:
                img = a.locator("img").first
                title = (img.get_attribute("alt") or "").strip()
            except Exception:
                title = ""

        results.append({"key": url, "url": url, "title": title})
        if len(results) >= max_items:
            break

    results = _dedup_keep_order(results)
    print("[list] found:", len(results))
    if not results:
        _save_debug(page, "list_no_results")
    return results


def scrape_gnb_click_then_collect(page, home_url: str, menu_text: str,
                                 include_patterns: list[str],
                                 max_items: int = DEFAULT_MAX_ITEMS) -> list[dict]:
    safe_goto(page, home_url, "home")

    candidates = [
        f'a:has-text("{menu_text}")',
        f'button:has-text("{menu_text}")',
        f'div:has-text("{menu_text}")',
        f'span:has-text("{menu_text}")',
    ]

    clicked = False
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                before = page.url
                print("[gnb] click", menu_text, "via", sel)
                loc.first.click(timeout=8000)
                clicked = True
                page.wait_for_timeout(800)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                after = page.url
                print("[gnb] url:", before, "->", after)
                break
            except Exception:
                pass

    if not clicked:
        _save_debug(page, f"gnb_click_fail_{menu_text}")
        return []

    return scrape_list_page_anchors(
        page,
        list_url=page.url,
        include_patterns=include_patterns,
        exclude_patterns=[],
        max_items=max_items,
    )


def scrape_main_banners_by_image_links(page, home_url: str, max_items: int = 20,
                                      restrict_same_host: bool = True) -> list[dict]:
    safe_goto(page, home_url, "home")
    page.wait_for_timeout(2500)

    base = page.url
    selectors = [
        "div[class*='banner'] a:has(img)",
        "div[class*='swiper'] a:has(img)",
        "div[class*='slick'] a:has(img)",
        "section a:has(img)",
        "a:has(img)",
    ]

    results = []
    seen = set()

    for sel in selectors:
        anchors = page.locator(sel).all()
        for a in anchors:
            href = a.get_attribute("href") or ""
            url = _abs_url(base, href)
            if not url:
                continue
            if restrict_same_host and not _same_host(base, url):
                continue
            if url in seen:
                continue

            title = ""
            try:
                img = a.locator("img").first
                title = (img.get_attribute("alt") or "").strip()
            except Exception:
                title = ""

            results.append({"key": url, "url": url, "title": title})
            seen.add(url)
            if len(results) >= max_items:
                break
        if len(results) >= max_items:
            break

    results = _dedup_keep_order(results)
    print("[banner] found:", len(results))
    if not results:
        _save_debug(page, "banner_no_results")
    return results


# -----------------------------
# 사이트별 스크레이퍼
# -----------------------------

def scrape_olens(page) -> list[dict]:
    EVENT_LIST_URL = "https://o-lens.com/event/list"
    safe_goto(page, EVENT_LIST_URL, "olens")

    cards = page.locator("div.board-information__wrapper")
    try:
        cards.first.wait_for(timeout=25000)
    except PlaywrightTimeoutError:
        _save_debug(page, "olens_list_timeout")
        return []

    total = cards.count()
    n = min(total, 15)
    print("[olens] cards:", total, "use:", n)

    results = []
    for i in range(n):
        card = cards.nth(i)
        title = ""
        try:
            title = (card.locator(".board-information__title").first.inner_text() or "").strip()
        except Exception:
            title = ""

        url = ""
        try:
            card.click(timeout=10000)
            page.wait_for_url(re.compile(r".*/event/view/.*"), timeout=25000)
            url = page.url
        except PlaywrightTimeoutError:
            _save_debug(page, f"olens_detail_timeout_{i+1}")
        finally:
            if "/event/view/" in page.url:
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=30000)
                    page.locator("div.board-information__wrapper").first.wait_for(timeout=20000)
                    cards = page.locator("div.board-information__wrapper")
                except Exception:
                    safe_goto(page, EVENT_LIST_URL, "olens_back")
                    page.locator("div.board-information__wrapper").first.wait_for(timeout=25000)
                    cards = page.locator("div.board-information__wrapper")

        if url:
            results.append({"key": url, "url": url, "title": title})

    return _dedup_keep_order(results)


def scrape_hapakristin(page) -> list[dict]:
    return scrape_gnb_click_then_collect(
        page,
        home_url="https://hapakristin.co.kr/",
        menu_text="진행 중인 이벤트",
        include_patterns=[r"hapakristin\.co\.kr/(collections|pages|products)/"],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_lensme(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://www.lens-me.com/shop/board.php?ps_bbscuid=17",
        include_patterns=[r"ps_mode=view", r"ps_uid=\d+"],
        exclude_patterns=[],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_i_sha(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://i-sha.kr/board/%EC%9D%B4%EB%B2%A4%ED%8A%B8/8/",
        include_patterns=[r"i-sha\.kr/board/"],
        exclude_patterns=[r"/page/\d+/?$"],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_lenbling(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://lenbling.com/board/event/8/",
        include_patterns=[r"lenbling\.com/board/event/"],
        exclude_patterns=[r"/board/event/8/?$"],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_yourly(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://yourly.kr/board/event",
        include_patterns=[r"yourly\.kr/board/event"],
        exclude_patterns=[r"/board/event/?$"],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_i_dol(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://www.i-dol.kr/bbs/event1.php",
        include_patterns=[r"i-dol\.kr/bbs/"],
        exclude_patterns=[r"event1\.php$"],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_shop_winc(page) -> list[dict]:
    results = scrape_gnb_click_then_collect(
        page,
        home_url="https://shop.winc.app/",
        menu_text="이벤트",
        include_patterns=[r"shop\.winc\.app/.*(event|promotion|board|bbs)"],
        max_items=DEFAULT_MAX_ITEMS,
    )
    if not results:
        results = [{"key": page.url, "url": page.url, "title": "이벤트(메뉴 이동)"}]
    return results


def scrape_ann365(page) -> list[dict]:
    safe_goto(page, "https://ann365.com/sub/menu.php", "ann365")

    # SALE 클릭 시도
    clicked_sale = False
    for sel in ['a:has-text("SALE")', 'button:has-text("SALE")', 'div:has-text("SALE")']:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                print("[ann365] click SALE via", sel)
                loc.first.click(timeout=8000)
                clicked_sale = True
                page.wait_for_timeout(2000)
                break
            except Exception:
                pass

    if not clicked_sale:
        print("[ann365] SALE click failed (continue)")

    return scrape_list_page_anchors(
        page,
        list_url=page.url,
        include_patterns=[r"ann365\.com/.*(event|이벤트|menu\.php)"],
        exclude_patterns=[],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_myfipn(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://www.myfipn.com/", max_items=20, restrict_same_host=True)


def scrape_chuulens(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://chuulens.kr/", max_items=20, restrict_same_host=True)


def scrape_gemhour(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://gemhour.co.kr/", max_items=20, restrict_same_host=True)


# -----------------------------
# Runner (사이트별 타임아웃)
# -----------------------------

SITES = [
    {"site": "O-Lens", "fn": scrape_olens},
    {"site": "Hapa Kristin", "fn": scrape_hapakristin},
    {"site": "Lens-me", "fn": scrape_lensme},
    {"site": "i-sha", "fn": scrape_i_sha},
    {"site": "shop.winc.app", "fn": scrape_shop_winc},
    {"site": "ann365", "fn": scrape_ann365},
    {"site": "lenbling", "fn": scrape_lenbling},
    {"site": "yourly", "fn": scrape_yourly},
    {"site": "i-dol", "fn": scrape_i_dol},
    {"site": "MYFiPN", "fn": scrape_myfipn},
    {"site": "CHUU Lens", "fn": scrape_chuulens},
    {"site": "Gemhour", "fn": scrape_gemhour},
]

def run_one_site_with_timeout(browser, site: str, fn):
    """
    각 사이트를 별도 스레드로 실행 + 타임아웃으로 강제 스킵
    """
    ctx = _new_context(browser)
    page = ctx.new_page()
    page.set_default_timeout(30000)

    try:
        return fn(page) or []
    finally:
        try:
            ctx.close()
        except Exception:
            pass

def main():
    state = load_state()
    if "seen" not in state:
        state["seen"] = {}

    print("[main] sites:", len(SITES))
    print("[main] loaded state sites:", list(state["seen"].keys()))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        any_state_changed = False

        for site_cfg in SITES:
            site = site_cfg["site"]
            fn = site_cfg["fn"]

            print("\n[main] site start:", site)

            items = []
            start = time.time()

            # 사이트별 하드 타임아웃
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(run_one_site_with_timeout, browser, site, fn)
                    items = fut.result(timeout=SITE_TIMEOUT_SEC)
            except FutureTimeoutError:
                print(f"[main] site timeout({SITE_TIMEOUT_SEC}s):", site)
                # 타임아웃 시에도 다음으로 진행
                items = []
            except Exception as e:
                print("[main] site error:", site, repr(e))
                items = []

            elapsed = time.time() - start
            items = _dedup_keep_order(items)
            print("[main] site done:", site, "scraped:", len(items), f"elapsed={elapsed:.1f}s")

            seen_set = set(state["seen"].get(site, []))

            if INIT_SILENT and not seen_set and items:
                for it in items:
                    k = it.get("key")
                    if k:
                        seen_set.add(k)
                state["seen"][site] = sorted(seen_set)
                any_state_changed = True
                print("[main] INIT_SILENT: initialized state only")
                continue

            new_items = [it for it in items if it.get("key") and it["key"] not in seen_set]
            print("[main] new:", len(new_items))

            if new_items:
                for it in new_items[:10]:
                    title = (it.get("title") or "").strip()
                    url = it.get("url") or ""
                    msg = f"[{site} 신규 프로모션]\n{title}\n{url}".strip()
                    post_slack(msg)
                    seen_set.add(it["key"])

                state["seen"][site] = sorted(seen_set)
                any_state_changed = True

        try:
            browser.close()
        except Exception:
            pass

    if any_state_changed:
        save_state(state)
        print("[main] state saved:", str(STATE_PATH))
    else:
        print("[main] no state changes")

if __name__ == "__main__":
    main()
