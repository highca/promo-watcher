import os
import re
import json
import time
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SLACK_WEBHOOK_URL_PROD = os.environ.get("SLACK_WEBHOOK_URL_PROD", "").strip()
SLACK_WEBHOOK_URL_TEST = os.environ.get("SLACK_WEBHOOK_URL_TEST", "").strip()

GITHUB_SERVER_URL = os.environ.get("GITHUB_SERVER_URL", "").strip()
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()
GITHUB_RUN_ID = os.environ.get("GITHUB_RUN_ID", "").strip()

STATE_PATH = Path("state/seen.json")
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

INIT_SILENT = False

DEFAULT_MAX_ITEMS = 30
NAV_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 20_000


def _stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def _run_url() -> str:
    if GITHUB_SERVER_URL and GITHUB_REPOSITORY and GITHUB_RUN_ID:
        return f"{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}"
    return ""


def post_slack(url: str, text: str):
    if not url:
        return
    resp = requests.post(url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def post_slack_prod(text: str):
    post_slack(SLACK_WEBHOOK_URL_PROD, text)


def post_slack_test(text: str):
    post_slack(SLACK_WEBHOOK_URL_TEST, text)


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


def _list_debug_files() -> set[str]:
    if not DEBUG_DIR.exists():
        return set()
    return set(p.name for p in DEBUG_DIR.glob("*") if p.is_file())


def _filter_site_debug_files(site_key: str, files: set[str]) -> list[str]:
    # debug prefix에 site_key가 들어가는 경우를 우선적으로 추출
    safe = re.sub(r"[^0-9A-Za-z가-힣]+", "_", site_key).strip("_")
    picked = []
    for f in sorted(files):
        if safe in f:
            picked.append(f)
    # site_key가 파일명에 안 들어가도 error_/list_no_results 같은 공용 파일이 생성될 수 있으니
    # 아무것도 못 찾으면 전체 새 파일명을 그대로 사용
    return picked if picked else sorted(files)


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


def new_page(browser, *, viewport_w=1200, viewport_h=800):
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={"width": viewport_w, "height": viewport_h},
    )
    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    return context, page


def try_close_common_popups(page):
    candidates = [
        'button:has-text("닫기")',
        'button:has-text("Close")',
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        'button:has-text("×")',
        'a:has-text("닫기")',
        'a:has-text("×")',
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                loc.first.click(timeout=1500)
                page.wait_for_timeout(300)
            except Exception:
                pass


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
    try_close_common_popups(page)

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


def scrape_main_banners_by_image_links(
    page,
    home_url: str,
    max_items: int = 20,
    restrict_same_host: bool = True,
) -> list[dict]:
    safe_goto(page, home_url, "home")
    try_close_common_popups(page)
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


def _hapakristin_find_event_list_url_from_home(page) -> str:
    anchors = page.locator("a[href]").all()
    base = page.url

    candidates = []
    for a in anchors:
        href = a.get_attribute("href") or ""
        url = _abs_url(base, href)
        if not url:
            continue
        if not _same_host(base, url):
            continue

        if re.search(r"hapakristin\.co\.kr/events/\d+", url):
            candidates.append(url)
            continue

        if re.search(r"hapakristin\.co\.kr/.{0,40}event", url, re.IGNORECASE):
            candidates.append(url)

    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=lambda x: (0 if re.search(r"/events/\d+", x) else 1, len(x)))
    return candidates[0] if candidates else ""


def scrape_hapakristin(page) -> list[dict]:
    home = "https://hapakristin.co.kr/"
    safe_goto(page, home, "hapakristin_home")
    try_close_common_popups(page)
    page.wait_for_timeout(2000)

    event_url = _hapakristin_find_event_list_url_from_home(page)
    if event_url:
        print("[hapakristin] found event url:", event_url)
        return scrape_list_page_anchors(
            page,
            list_url=event_url,
            include_patterns=[r"hapakristin\.co\.kr/(collections|pages|products|events)/"],
            exclude_patterns=[],
            max_items=DEFAULT_MAX_ITEMS,
        )

    guesses = [
        "https://hapakristin.co.kr/events",
        "https://hapakristin.co.kr/pages/event",
    ]

    for g in guesses:
        try:
            safe_goto(page, g, "hapakristin_guess")
            try_close_common_popups(page)
            page.wait_for_timeout(1500)
            items = scrape_list_page_anchors(
                page,
                list_url=page.url,
                include_patterns=[r"hapakristin\.co\.kr/(collections|pages|products|events)/"],
                exclude_patterns=[],
                max_items=DEFAULT_MAX_ITEMS,
            )
            if items:
                return items
        except Exception:
            pass

    _save_debug(page, "hapakristin_event_discovery_fail")
    return []


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


def scrape_myfipn(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://www.myfipn.com/", max_items=20, restrict_same_host=True)


def scrape_chuulens(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://chuulens.kr/", max_items=20, restrict_same_host=True)


def scrape_gemhour(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://gemhour.co.kr/", max_items=20, restrict_same_host=True)


def scrape_shop_winc(page) -> list[dict]:
    home = "https://shop.winc.app/"
    safe_goto(page, home, "winc_home")
    page.wait_for_timeout(3500)

    anchors = page.locator('a[href]').all()
    results = []
    seen = set()

    for a in anchors:
        href = a.get_attribute("href") or ""
        url = _abs_url(page.url, href)
        if not url:
            continue
        if not _same_host(page.url, url):
            continue
        m = re.search(r"/event/(\d+)", url)
        if not m:
            continue
        if url in seen:
            continue

        title = (a.inner_text() or "").strip() or f"event/{m.group(1)}"
        key = f"winc:event:{m.group(1)}"
        results.append({"key": key, "url": url, "title": title})
        seen.add(url)
        if len(results) >= DEFAULT_MAX_ITEMS:
            break

    results = _dedup_keep_order(results)
    print("[winc] events:", len(results))
    if not results:
        _save_debug(page, "winc_no_event_links")
        results = [{"key": "winc:home", "url": home, "title": "이벤트(홈)"}]

    return results


def scrape_ann365(page) -> list[dict]:
    safe_goto(page, "https://ann365.com/sub/menu.php", "ann365")
    try_close_common_popups(page)
    page.wait_for_timeout(1500)

    return scrape_list_page_anchors(
        page,
        list_url=page.url,
        include_patterns=[r"ann365\.com/.*(prd_event=|event|이벤트|menu\.php|product/list\.php)"],
        exclude_patterns=[],
        max_items=DEFAULT_MAX_ITEMS,
    )


SITES = [
    {"site": "O-Lens", "display": "오렌즈", "mode": "normal", "fn": scrape_olens},
    {"site": "Hapa Kristin", "display": "하파크리스틴", "mode": "desktop", "fn": scrape_hapakristin},
    {"site": "Lens-me", "display": "렌즈미", "mode": "normal", "fn": scrape_lensme},
    {"site": "MYFiPN", "display": "마이핍앤", "mode": "normal", "fn": scrape_myfipn},
    {"site": "CHUU Lens", "display": "츄렌즈", "mode": "normal", "fn": scrape_chuulens},
    {"site": "Gemhour", "display": "젬아워", "mode": "normal", "fn": scrape_gemhour},
    {"site": "i-sha", "display": "아이샤", "mode": "normal", "fn": scrape_i_sha},
    {"site": "shop.winc.app", "display": "윙크", "mode": "normal", "fn": scrape_shop_winc},
    {"site": "ann365", "display": "앤365", "mode": "normal", "fn": scrape_ann365},
    {"site": "lenbling", "display": "렌블링", "mode": "normal", "fn": scrape_lenbling},
    {"site": "yourly", "display": "유얼리", "mode": "normal", "fn": scrape_yourly},
    {"site": "i-dol", "display": "아이돌렌즈", "mode": "normal", "fn": scrape_i_dol},
]


def main():
    state = load_state()
    if "seen" not in state:
        state["seen"] = {}

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
            site_key = cfg["site"]
            site_name = cfg["display"]
            mode = cfg["mode"]
            fn = cfg["fn"]

            print("\n[main] site:", site_key, "(", site_name, ")")

            # 사이트 실행 전 debug 파일 목록 스냅샷
            debug_before = _list_debug_files()

            start = time.time()
            context = None
            page = None
            items = []
            site_exception = ""

            try:
                if mode == "desktop":
                    context, page = new_page(browser, viewport_w=1400, viewport_h=900)
                else:
                    context, page = new_page(browser, viewport_w=1200, viewport_h=800)

                items = fn(page) or []
                items = _dedup_keep_order(items)

            except Exception as e:
                site_exception = repr(e)
                print("[main] site error:", site_key, site_exception)
                if page is not None:
                    _save_debug(page, f"error_{site_key.replace(' ', '_')}")
                items = []

            finally:
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass

            elapsed = time.time() - start
            print("[main] scraped:", len(items), f"elapsed={elapsed:.1f}s")

            # 사이트 실행 후 debug 파일 목록 비교 → 새 파일 있을 때만 경고
            debug_after = _list_debug_files()
            new_debug_files = debug_after - debug_before

            if new_debug_files:
                run_url = _run_url()
                picked = _filter_site_debug_files(site_key, new_debug_files)
                # 너무 길면 8개만
                picked = picked[:8]
                reason = "debug 파일이 새로 생성되었습니다(수집 실패/구조 변경 가능)."
                if site_exception:
                    reason = f"예외로 debug 생성: {site_exception}"

                msg = (
                    f"[수집 경고] {site_name}\n"
                    f"{reason}\n"
                    f"새 debug: {', '.join(picked)}\n"
                    f"Run: {run_url}"
                ).strip()

                try:
                    post_slack_test(msg)
                except Exception as e:
                    print("[main] test slack notify failed:", repr(e))

            seen_set = set(state["seen"].get(site_key, []))

            if INIT_SILENT and not seen_set and items:
                for it in items:
                    k = it.get("key")
                    if k:
                        seen_set.add(k)
                state["seen"][site_key] = sorted(seen_set)
                any_state_changed = True
                print("[main] INIT_SILENT: initialized state only")
                continue

            new_items = [it for it in items if it.get("key") and it["key"] not in seen_set]
            print("[main] new:", len(new_items))

            # 신규 알림은 운영 채널로만
            if new_items:
                for it in new_items[:10]:
                    title = (it.get("title") or "").strip()
                    url = it.get("url") or ""
                    msg = f"[{site_name} 신규 프로모션]\n{title}\n{url}".strip()
                    post_slack_prod(msg)
                    seen_set.add(it["key"])

                state["seen"][site_key] = sorted(seen_set)
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
