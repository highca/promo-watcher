import os
import re
import json
import time
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

STATE_PATH = Path("state/seen.json")
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 운영 시작 시 False 권장 (첫 실행 도배 방지용은 True)
INIT_SILENT = False

DEFAULT_MAX_ITEMS = 30

# “한 사이트에서 너무 오래 끄는 느낌”을 줄이기 위한 기본 제한(밀리초)
NAV_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 20_000


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


def safe_goto(page, url: str, label: str = ""):
    print(f"[goto]{'['+label+']' if label else ''} {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(1200)


def new_page(browser):
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={"width": 750, "height": 1500},
    )
    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    return context, page


# -----------------------------
# 공용 수집기
# -----------------------------

def scrape_list_page_anchors(
    page,
    list_url: str,
    include_patterns: list[str],
    exclude_patterns: list[str] | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict]:
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


def scrape_gnb_click_then_collect(
    page,
    home_url: str,
    menu_text: str,
    include_patterns: list[str],
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict]:
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
                loc.first.click(timeout=8_000)
                clicked = True
                page.wait_for_timeout(800)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
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


def scrape_main_banners_by_image_links(
    page,
    home_url: str,
    max_items: int = 20,
    restrict_same_host: bool = True,
) -> list[dict]:
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
    url = "https://o-lens.com/event/list"
    safe_goto(page, url, "olens")

    cards = page.locator("div.board-information__wrapper")
    try:
        cards.first.wait_for(timeout=25_000)
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

        detail_url = ""
        try:
            card.click(timeout=10_000)
            page.wait_for_url(re.compile(r".*/event/view/.*"), timeout=25_000)
            detail_url = page.url
        except PlaywrightTimeoutError:
            _save_debug(page, f"olens_detail_timeout_{i+1}")
        finally:
            if "/event/view/" in page.url:
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=30_000)
                    page.locator("div.board-information__wrapper").first.wait_for(timeout=20_000)
                except Exception:
                    safe_goto(page, url, "olens_back")

        if detail_url:
            results.append({"key": detail_url, "url": detail_url, "title": title})

    return _dedup_keep_order(results)


def scrape_hapakristin(page) -> list[dict]:
    return scrape_gnb_click_then_collect(
        page,
        home_url="https://hapakristin.co.kr/",
        menu_text="진행 중인 이벤트",
        include_patterns=[r"hapakristin\.co\.kr/(collections|pages|products|events)/"],
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
        exclude_patterns=[],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_ann365(page) -> list[dict]:
    safe_goto(page, "https://ann365.com/sub/menu.php", "ann365")

    clicked_sale = False
    for sel in ['a:has-text("SALE")', 'button:has-text("SALE")', 'div:has-text("SALE")']:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                print("[ann365] click SALE via", sel)
                loc.first.click(timeout=8_000)
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
        include_patterns=[r"ann365\.com/.*(event|이벤트|prd_event=|menu\.php)"],
        exclude_patterns=[],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_myfipn(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://www.myfipn.com/", max_items=20, restrict_same_host=True)


def scrape_chuulens(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://chuulens.kr/", max_items=20, restrict_same_host=True)


def scrape_gemhour(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://gemhour.co.kr/", max_items=20, restrict_same_host=True)


def scrape_shop_winc(page) -> list[dict]:
    """
    shop.winc.app:
    Flutter Web이라 headless에서 화면이 하얗게 렌더링될 수 있음.
    HTML에 SEO용으로 숨겨진 nav(display:none)에 /event/{id} 링크가 포함되는 경우가 있어,
    클릭 대신 /event/ 링크를 직접 수집한다.
    """
    home = "https://shop.winc.app/"
    safe_goto(page, home, "winc_home")

    anchors = page.locator('a[href^="/event/"]').all()
    print("[winc] /event anchors:", len(anchors))

    results = []
    for a in anchors[:DEFAULT_MAX_ITEMS]:
        href = a.get_attribute("href") or ""
        if not href:
            continue
        url = _abs_url(page.url, href)
        m = re.search(r"/event/(\d+)", url)
        if not m:
            continue

        title = (a.inner_text() or "").strip() or f"event/{m.group(1)}"
        key = f"winc:event:{m.group(1)}"
        results.append({"key": key, "url": url, "title": title})

    results = _dedup_keep_order(results)
    if not results:
        _save_debug(page, "winc_no_event_links")
        # fallback: 홈 자체라도 키로 저장(변경 감지 보조)
        results = [{"key": "winc:home", "url": home, "title": "이벤트(홈)"}]

    return results


# -----------------------------
# Runner (스레드 없이 순차 실행)
# -----------------------------

SITES = [
    {"site": "olens", "display": "O-Lens", "fn": scrape_olens},
    {"site": "hapakristin", "display": "Hapa Kristin", "fn": scrape_hapakristin},
    {"site": "lensme", "display": "Lens-me", "fn": scrape_lensme},
    {"site": "myfipn", "display": "MYFiPN", "fn": scrape_myfipn},
    {"site": "chuulens", "display": "CHUU LENS", "fn": scrape_chuulens},
    {"site": "gemhour", "display": "Gemhour", "fn": scrape_gemhour},
    {"site": "isha", "display": "i-sha", "fn": scrape_i_sha},
    {"site": "winc", "display": "Winc", "fn": scrape_shop_winc},
    {"site": "ann365", "display": "ANN365", "fn": scrape_ann365},
    {"site": "lenbling", "display": "Lenbling", "fn": scrape_lenbling},
    {"site": "yourly", "display": "Yourly", "fn": scrape_yourly},
    {"site": "idol", "display": "i-dol", "fn": scrape_i_dol},
]


def main():
    state = load_state()
    if "seen" not in state:
        state["seen"] = {}

    print("[main] loaded state sites:", list(state["seen"].keys()))
    any_state_changed = False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        for cfg in SITES:
            site = cfg["site"]
            fn = cfg["fn"]

            print("\n[main] site:", site)
            start = time.time()

            context = None
            page = None
            items = []

            try:
                context, page = new_page(browser)
                items = fn(page) or []
            except Exception as e:
                print("[main] site error:", site, repr(e))
                if page is not None:
                    _save_debug(page, f"error_{site.replace(' ', '_')}")
                items = []
            finally:
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass

            elapsed = time.time() - start
            items = _dedup_keep_order(items)
            print("[main] scraped:", len(items), f"elapsed={elapsed:.1f}s")

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
