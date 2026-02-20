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

# 첫 실행 시 상태만 저장하고 알림은 보내지 않음 (이미 state가 있으면 False 권장)
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
    safe = re.sub(r"[^0-9A-Za-z가-힣]+", "_", site_key).strip("_")
    picked = []
    for f in sorted(files):
        if safe and safe in f:
            picked.append(f)
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


def _save_debug_text(prefix: str, text: str):
    stamp = _stamp()
    p = DEBUG_DIR / f"{prefix}_{stamp}.txt"
    try:
        p.write_text(text, encoding="utf-8")
    except Exception:
        pass
    print("[debug] saved", str(p))


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
            # 이미지 alt도 제목 후보
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


def _event_id_from_url(u: str) -> int:
    m = re.search(r"/events/(\d+)", u)
    return int(m.group(1)) if m else 0


def _collect_hapakristin_events_excluding_footer(page) -> list[str]:
    """
    하파크리스틴은 홈 전체에서 events를 긁으면 footer/기타 영역의 과거 이벤트(175/176 등)가 섞입니다.
    따라서 DOM에서 footer 내부 링크는 제외하고 /events/<id>만 수집합니다.
    """
    hrefs: list[str] = page.evaluate(
        """() => {
            const out = [];
            const as = Array.from(document.querySelectorAll('a[href]'));
            for (const a of as) {
              if (a.closest('footer')) continue; // footer 제외
              const href = a.getAttribute('href') || '';
              out.push(href);
            }
            return out;
        }"""
    )

    base = page.url
    urls = []
    for href in hrefs:
        url = (href or "").strip()
        if not url:
            continue
        absu = urljoin(base, url)
        if not absu:
            continue
        if not _same_host(base, absu):
            continue
        if re.search(r"^https://hapakristin\.co\.kr/events/\d+/?$", absu):
            urls.append(absu.rstrip("/"))

    urls = list(dict.fromkeys(urls))
    return urls


def _find_anchor_href_by_text(page, patterns: list[str]) -> str:
    """
    텍스트 기반으로 메뉴 링크를 찾습니다. (예: '진행 중인 이벤트')
    실패 시 빈 문자열 반환.
    """
    # 접근성/번역 차이를 고려해 여러 패턴 지원
    for pat in patterns:
        loc = page.locator(f'a:has-text("{pat}")')
        try:
            if loc.count() > 0:
                href = loc.first.get_attribute("href") or ""
                absu = _abs_url(page.url, href)
                if absu:
                    return absu
        except Exception:
            pass
    return ""


def scrape_hapakristin(page) -> list[dict]:
    """
    핵심 변경점:
    1) 홈 전체에서 events를 긁지 않고,
    2) '진행 중인 이벤트' 메뉴가 가리키는 페이지로 진입한 뒤,
    3) 그 페이지(및 footer 제외 영역)에서만 /events/<id>를 수집합니다.
    """
    home = "https://hapakristin.co.kr/"
    safe_goto(page, home, "hapakristin_home")
    try_close_common_popups(page)
    page.wait_for_timeout(2000)

    # 1) '진행 중인 이벤트' 링크 찾기 (메뉴가 클릭/호버형이어도 href가 있는 경우가 많음)
    start_url = _find_anchor_href_by_text(page, ["진행 중인 이벤트", "진행중인 이벤트"])
    if start_url:
        safe_goto(page, start_url, "hapakristin_ongoing_entry")
        try_close_common_popups(page)
        page.wait_for_timeout(1500)
    else:
        # 못 찾으면, 기존처럼 홈에서 footer 제외 + events만 수집 (최후 fallback)
        _save_debug(page, "hapakristin_ongoing_menu_not_found")

    # 2) 현재 페이지에서 footer 제외 + /events/<id> 수집
    event_urls = _collect_hapakristin_events_excluding_footer(page)

    # 3) 여기까지도 0건이면 알려진 진행중 이벤트 페이지(사용자가 제공한 두 URL 중 하나)에서 재시도
    if not event_urls:
        for fallback in ["https://hapakristin.co.kr/events/6824", "https://hapakristin.co.kr/events/6724"]:
            try:
                safe_goto(page, fallback, "hapakristin_fallback_known")
                try_close_common_popups(page)
                page.wait_for_timeout(1200)
                event_urls = _collect_hapakristin_events_excluding_footer(page)
                if event_urls:
                    break
            except Exception:
                pass

    # 4) 중복 제거 + 정렬(큰 id 우선) + 상한
    event_urls = list(dict.fromkeys(event_urls))
    event_urls.sort(key=_event_id_from_url, reverse=True)
    event_urls = event_urls[:20]

    if event_urls:
        print("[hapakristin] collected ids:", [_event_id_from_url(u) for u in event_urls])

    results = [{"key": u, "url": u, "title": f"이벤트 {_event_id_from_url(u)}"} for u in event_urls]
    print("[hapakristin] events found:", len(results))
    if not results:
        _save_debug(page, "hapakristin_events_no_results")
    return results


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

    anchors = page.locator("a[href]").all()
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


def _requests_get(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
    r.raise_for_status()
    return r.text


def scrape_ann365(page) -> list[dict]:
    """
    1) requests로 menu.php HTML 확보 시도
    2) 실패하면 Playwright로 같은 페이지에서 content() 확보 시도
    3) HTML에서 prd_event= 링크를 추출해 모니터링 대상으로 반환
    """
    menu_url = "https://ann365.com/sub/menu.php"
    html = ""

    # 1) requests
    try:
        html = _requests_get(menu_url)
    except Exception as e:
        _save_debug_text("ann365_request_fail", f"{menu_url}\n{repr(e)}")

    # 2) playwright fallback
    if not html:
        try:
            safe_goto(page, menu_url, "ann365_menu_pw")
            try_close_common_popups(page)
            page.wait_for_timeout(1500)
            html = page.content() or ""
        except Exception as e:
            _save_debug(page, "ann365_playwright_fail")
            _save_debug_text("ann365_playwright_fail_reason", f"{menu_url}\n{repr(e)}")
            html = ""

    if not html or len(html.strip()) < 50:
        # 여전히 비어있으면 실패로 간주(경고는 debug 생성으로 처리됨)
        return []

    ids = re.findall(r"prd_event=(\d+)", html)
    ids = list(dict.fromkeys(ids))

    if not ids:
        # href 전체에서 prd_event 재탐색
        hrefs = re.findall(r'href=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
        for h in hrefs:
            if "prd_event=" in h:
                mm = re.search(r"prd_event=(\d+)", h)
                if mm:
                    ids.append(mm.group(1))
        ids = list(dict.fromkeys(ids))

    if not ids:
        _save_debug_text("ann365_no_prd_event", "menu.php에서 prd_event 링크를 찾지 못했습니다.")
        return []

    results = []
    for ev_id in ids[:10]:
        url = f"https://ann365.com/product/list.php?prd_event={ev_id}"
        results.append({"key": f"ann365:prd_event:{ev_id}", "url": url, "title": f"이벤트 {ev_id}"})

    print("[ann365] prd_event found:", len(results), ids[:10])
    return results


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

            debug_after = _list_debug_files()
            new_debug_files = debug_after - debug_before

            # debug 파일이 새로 생성된 경우에만 테스트 채널 경고
            if new_debug_files:
                run_url = _run_url()
                picked = _filter_site_debug_files(site_key, new_debug_files)[:8]
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
