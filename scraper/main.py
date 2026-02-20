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
    out, s = [], set()
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
    picked = [f for f in sorted(files) if safe and safe in f]
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


def safe_goto(page, url: str, label: str = "", wait: str = "domcontentloaded"):
    print(f"[goto]{'['+label+']' if label else ''} {url}")
    page.goto(url, wait_until=wait, timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(1200)


def new_page(browser, *, viewport_w=1200, viewport_h=800):
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={"width": viewport_w, "height": viewport_h},
    )

    # headless 탐지 회피(가벼운 수준)
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """
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


def scrape_list_page_anchors(page, list_url: str, include_patterns: list[str],
                            exclude_patterns: list[str] | None = None,
                            max_items: int = DEFAULT_MAX_ITEMS) -> list[dict]:
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


def scrape_main_banners_by_image_links(page, home_url: str, max_items: int = 20,
                                      restrict_same_host: bool = True) -> list[dict]:
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

    results, seen = [], set()
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


def _hapakristin_try_open_ongoing(page) -> bool:
    """
    hover로 하위 메뉴가 생성되는 경우를 최대한 커버:
    - header/nav 영역에서 '이벤트'에 hover
    - '진행 중인 이벤트' 클릭
    """
    try:
        # '이벤트' 후보를 넓게 잡고, 화면에 보이는(first) 것을 hover
        evt_candidates = page.locator("header, nav").locator("text=이벤트")
        if evt_candidates.count() == 0:
            return False

        evt = evt_candidates.first
        evt.hover(timeout=6_000)
        page.wait_for_timeout(700)

        ongoing = page.locator("text=진행 중인 이벤트").first
        ongoing.click(timeout=6_000)
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1200)
        return True
    except Exception:
        return False


def _hapakristin_collect_event_links_strict(page) -> list[str]:
    """
    하파크리스틴 오수집을 줄이기 위한 '강한' 수집:
    - header/nav/footer 안 링크 제외
    - 이전/다음 네비 링크 제외
    - 이미지(img)를 포함한 a 태그만 우선 수집 (이벤트 배너/카드형)
    - /events/<id> 링크만
    """
    data = page.evaluate(
        """() => {
            const out = [];
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            for (const a of anchors) {
              if (a.closest('header, nav, footer')) continue;

              const t = (a.innerText || '').trim();
              if (t.includes('이전') || t.includes('다음')) continue;

              const cls = (a.className || '').toString().toLowerCase();
              const aria = (a.getAttribute('aria-label') || '').toLowerCase();
              if (cls.includes('prev') || cls.includes('next') || aria.includes('prev') || aria.includes('next')) continue;

              // 이벤트 배너/카드 성격: 이미지 포함 링크 우선
              const hasImg = a.querySelector('img') !== null;
              const href = a.getAttribute('href') || '';

              out.push({href, hasImg});
            }
            return out;
        }"""
    )

    base = page.url
    img_urls = []
    text_urls = []

    for row in data:
        href = (row.get("href") or "").strip()
        if not href:
            continue
        absu = _abs_url(base, href)
        if not absu or not _same_host(base, absu):
            continue
        if not re.search(r"^https://hapakristin\.co\.kr/events/\d+/?$", absu):
            continue

        if row.get("hasImg"):
            img_urls.append(absu.rstrip("/"))
        else:
            text_urls.append(absu.rstrip("/"))

    # 이미지 포함 링크가 있으면 그것만 사용 (오수집 억제)
    urls = img_urls if img_urls else text_urls

    urls = list(dict.fromkeys(urls))
    urls.sort(key=_event_id_from_url, reverse=True)
    return urls[:20]


def scrape_hapakristin(page) -> list[dict]:
    safe_goto(page, "https://hapakristin.co.kr/", "hapakristin_home")
    try_close_common_popups(page)
    page.wait_for_timeout(1200)

    ok = _hapakristin_try_open_ongoing(page)
    if not ok:
        _save_debug(page, "hapakristin_ongoing_menu_not_found")
        # 사용자 확인된 진행중 이벤트 페이지로 fallback
        safe_goto(page, "https://hapakristin.co.kr/events/6824", "hapakristin_fallback_6824")
        try_close_common_popups(page)
        page.wait_for_timeout(1200)

    # 1차: 현재 페이지에서 엄격 수집
    event_urls = _hapakristin_collect_event_links_strict(page)

    # 2차: 그래도 0이면 6724에서 재시도
    if not event_urls:
        safe_goto(page, "https://hapakristin.co.kr/events/6724", "hapakristin_fallback_6724")
        try_close_common_popups(page)
        page.wait_for_timeout(1200)
        event_urls = _hapakristin_collect_event_links_strict(page)

    if event_urls:
        print("[hapakristin] collected ids:", [_event_id_from_url(u) for u in event_urls])
    else:
        _save_debug(page, "hapakristin_no_event_links")

    results = [{"key": u, "url": u, "title": f"이벤트 {_event_id_from_url(u)}"} for u in event_urls]
    print("[hapakristin] events found:", len(results))
    return results


def scrape_lensme(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://www.lens-me.com/shop/board.php?ps_bbscuid=17",
        include_patterns=[r"ps_mode=view", r"ps_uid=\d+"],
    )


def scrape_i_sha(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://i-sha.kr/board/%EC%9D%B4%EB%B2%A4%ED%8A%B8/8/",
        include_patterns=[r"i-sha\.kr/board/"],
        exclude_patterns=[r"/page/\d+/?$"],
    )


def scrape_lenbling(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://lenbling.com/board/event/8/",
        include_patterns=[r"lenbling\.com/board/event/"],
        exclude_patterns=[r"/board/event/8/?$"],
    )


def scrape_yourly(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://yourly.kr/board/event",
        include_patterns=[r"yourly\.kr/board/event"],
        exclude_patterns=[r"/board/event/?$"],
    )


def scrape_i_dol(page) -> list[dict]:
    return scrape_list_page_anchors(
        page,
        list_url="https://www.i-dol.kr/bbs/event1.php",
        include_patterns=[r"i-dol\.kr/bbs/"],
    )


def scrape_myfipn(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://www.myfipn.com/")


def scrape_chuulens(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://chuulens.kr/")


def scrape_gemhour(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://gemhour.co.kr/")


def scrape_shop_winc(page) -> list[dict]:
    home = "https://shop.winc.app/"
    safe_goto(page, home, "winc_home")
    page.wait_for_timeout(2500)

    anchors = page.locator("a[href]").all()
    results, seen = [], set()

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


def _jina_proxy_url(url: str) -> str:
    # 예: https://r.jina.ai/https://ann365.com/sub/menu.php
    return "https://r.jina.ai/" + url


def _extract_ann365_prd_event_ids(html: str) -> list[str]:
    ids = re.findall(r"prd_event=(\d+)", html)
    ids = list(dict.fromkeys(ids))
    if ids:
        return ids

    hrefs = re.findall(r'href=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
    for h in hrefs:
        mm = re.search(r"prd_event=(\d+)", h)
        if mm:
            ids.append(mm.group(1))
    return list(dict.fromkeys(ids))


def scrape_ann365(page) -> list[dict]:
    menu_url = "https://ann365.com/sub/menu.php"
    html = ""

    # 1) requests
    try:
        html = _requests_get(menu_url)
    except Exception as e:
        _save_debug_text("ann365_request_fail", f"{menu_url}\n{repr(e)}")

    # 2) jina 프록시
    if not html:
        try:
            html = _requests_get(_jina_proxy_url(menu_url))
        except Exception as e:
            _save_debug_text("ann365_jina_fail", f"{_jina_proxy_url(menu_url)}\n{repr(e)}")

    # 3) playwright (networkidle)
    if not html:
        try:
            safe_goto(page, menu_url, "ann365_menu_pw", wait="domcontentloaded")
            try_close_common_popups(page)
            try:
                page.wait_for_load_state("networkidle", timeout=25_000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            html = page.content() or ""
        except Exception as e:
            _save_debug(page, "ann365_playwright_fail")
            _save_debug_text("ann365_playwright_fail_reason", f"{menu_url}\n{repr(e)}")
            return []

    if not html or len(html.strip()) < 200:
        _save_debug(page, "ann365_empty_html")
        _save_debug_text("ann365_empty_html_info", f"len={len(html.strip())}")
        return []

    ids = _extract_ann365_prd_event_ids(html)
    if not ids:
        _save_debug(page, "ann365_no_prd_event")
        _save_debug_text("ann365_no_prd_event_snippet", html[:2000])
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
