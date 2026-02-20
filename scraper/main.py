import os
import re
import json
import time
import requests
from pathlib import Path
from datetime import datetime, date
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


def _parse_date_yyyymmdd(text: str) -> date | None:
    # 2026.02.20 / 2026-02-20 / 2026/02/20
    m = re.search(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", text)
    if not m:
        return None
    y = int(m.group(1))
    mo = int(m.group(2))
    d = int(m.group(3))
    try:
        return date(y, mo, d)
    except Exception:
        return None


def _extract_date_range(text: str) -> tuple[date | None, date | None]:
    # 가장 흔한 범위 표기: 2026.02.01 ~ 2026.02.29 (또는 -)
    # 텍스트 내 날짜 2개를 찾아서 (start,end)로 반환
    dates = re.findall(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", text)
    parsed = []
    for y, mo, d in dates[:6]:
        try:
            parsed.append(date(int(y), int(mo), int(d)))
        except Exception:
            pass
    if len(parsed) >= 2:
        return parsed[0], parsed[1]
    if len(parsed) == 1:
        return parsed[0], None
    return None, None


def hapakristin_is_active_event(page, url: str) -> bool:
    safe_goto(page, url, "hapakristin_event_check")
    try_close_common_popups(page)
    page.wait_for_timeout(1200)

    # 1) 비활성/404/존재하지 않음 신호
    try:
        body_text = (page.inner_text("body") or "").strip()
    except Exception:
        body_text = ""

    invalid_keywords = [
        "존재하지", "찾을 수", "404", "not found", "페이지를 찾을 수",
        "잘못된 접근", "유효하지", "오류가 발생",
    ]
    if any(k.lower() in body_text.lower() for k in invalid_keywords):
        return False

    # 2) 종료/마감 신호
    closed_keywords = ["종료", "마감", "종료된", "종료되었습니다", "이벤트 종료", "마감되었습니다"]
    if any(k in body_text for k in closed_keywords):
        return False

    # 3) 기간 파싱 가능하면, 종료일이 오늘 이전이면 비활성
    # (표현이 다양한데, 날짜가 2개 이상 있으면 보통 start~end로 쓰입니다)
    start_dt, end_dt = _extract_date_range(body_text)
    today = date.today()

    if end_dt is not None:
        if end_dt < today:
            return False
        return True

    # end_dt가 없고 날짜가 하나만 있다면, 그 날짜가 “종료일”인지 알기 어려워서
    # 여기서는 보수적으로 “활성으로 간주”하되, 필요하면 규칙 강화 가능
    return True


def scrape_hapakristin(page) -> list[dict]:
    home = "https://hapakristin.co.kr/"
    safe_goto(page, home, "hapakristin_home")
    try_close_common_popups(page)
    page.wait_for_timeout(2000)

    base = page.url
    anchors = page.locator("a[href]").all()

    # 1차: /events/<id> 링크만 수집
    event_urls = []
    for a in anchors:
        href = a.get_attribute("href") or ""
        url = _abs_url(base, href)
        if not url:
            continue
        if not _same_host(base, url):
            continue
        if re.search(r"^https://hapakristin\.co\.kr/events/\d+/?$", url):
            event_urls.append(url.rstrip("/"))

    # 중복 제거 + 검사 상한(속도)
    event_urls = list(dict.fromkeys(event_urls))[:20]

    # 디버깅용: 실제로 어떤 event id들이 수집되는지 로그에 남김
    if event_urls:
        ids = [_event_id_from_url(u) for u in event_urls]
        print("[hapakristin] collected ids:", ids)

    # 2차: 각 이벤트 페이지 진입해서 활성만 남김
    active_urls = []
    for u in event_urls:
        try:
            if hapakristin_is_active_event(page, u):
                active_urls.append(u)
        except Exception as e:
            _save_debug(page, "hapakristin_event_check_fail")
            _save_debug_text("hapakristin_event_check_fail_reason", repr(e))

    active_urls = list(dict.fromkeys(active_urls))
    active_urls.sort(key=_event_id_from_url, reverse=True)

    results = [{"key": u, "url": u, "title": f"이벤트 {_event_id_from_url(u)}"} for u in active_urls]
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
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.text


def scrape_ann365_requests() -> list[dict]:
    """
    ann365는 Playwright에서 about:blank/빈 DOM이 나올 수 있어 requests로 처리.
    핵심은 menu.php에서 prd_event= 파라미터를 가진 링크를 찾아 그 이벤트 id를 모니터링하는 것.
    """
    menu_url = "https://ann365.com/sub/menu.php"
    try:
        html = _requests_get(menu_url)
    except Exception as e:
        _save_debug_text("ann365_request_fail", f"menu fetch fail: {repr(e)}")
        return []

    # prd_event= 숫자 추출
    ids = re.findall(r"prd_event=(\d+)", html)
    ids = list(dict.fromkeys(ids))

    # 그래도 못 찾으면, menu.php 안에 product/list.php 링크가 있을 수 있으니 href 자체로 추출
    if not ids:
        m = re.findall(r'href=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
        for href in m:
            if "prd_event=" in href:
                mm = re.search(r"prd_event=(\d+)", href)
                if mm:
                    ids.append(mm.group(1))
        ids = list(dict.fromkeys(ids))

    if not ids:
        _save_debug_text("ann365_no_prd_event", "menu.php에서 prd_event 링크를 찾지 못했습니다.")
        return []

    # 가장 대표 이벤트(보통 1개)만 쓰되, 여러 개면 전부 모니터링도 가능
    # 여기서는 전부 반환 (향후 이벤트 탭 여러 개일 수도 있어서)
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
    # ann365는 requests 기반이므로 mode는 무시되지만 통일감 있게 normal로 둠
    {"site": "ann365", "display": "앤365", "mode": "normal", "fn": lambda page: scrape_ann365_requests()},
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
                # ann365는 requests라 page가 필요 없지만, 다른 사이트와 동일 흐름 유지를 위해 page를 만듭니다.
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
