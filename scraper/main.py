# scraper/main.py
# 신규만 운영채널 알림, debug 신규 생성 시에만 테스트채널 경고
#
# 핵심 동작
# - 운영 채널: "신규 프로모션"만 알림
# - 테스트 채널: "debug 파일이 새로 생성된 경우에만" 경고 알림
#
# 주요 보완(하파크리스틴)
# - hover 실패는 정상 fallback으로 취급 (debug 생성 X)
# - 고정 URL(6824/6724) 체크 시:
#   * HTTP status None은 정상일 수 있으므로 실패로 보지 않음
#   * status >= 400 또는 페이지 내용이 명백히 에러(404/Not Found 등)일 때만 debug 생성
#
# ann365
# - contact_event 허브 기반
# - 연속 2페이지 무수집이면 조기 종료하여 실행 시간 단축

import os
import re
import json
import time
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# =========================
# 환경변수/설정
# =========================
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


# =========================
# 공용 유틸
# =========================
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
    """
    페이지 이동 + HTTP status 반환(가능한 경우)
    - 반환값이 None이면 status를 못 받은 경우(redirect/특수 응답 등)
    """
    print(f"[goto]{'['+label+']' if label else ''} {url}")
    resp = page.goto(url, wait_until=wait, timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(1000)
    status = None
    try:
        status = resp.status if resp is not None else None
    except Exception:
        status = None
    return status


def new_page(browser, *, viewport_w=1200, viewport_h=800):
    context = browser.new_context(
        user_agent=USER_AGENT,
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        viewport={"width": viewport_w, "height": viewport_h},
    )
    # headless 탐지 완화(가벼운 수준)
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
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


def page_looks_like_error(page) -> bool:
    """
    status를 못 받는(None) 케이스가 있어도
    페이지 자체가 404/Not Found 등 명확한 에러 페이지면 실패로 간주하기 위한 휴리스틱
    """
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    try:
        html = (page.content() or "").lower()
    except Exception:
        html = ""

    needles = ["404", "not found", "페이지를 찾을 수", "존재하지", "error", "접근할 수"]
    if any(n in title for n in needles):
        return True
    if any(n in html for n in needles):
        return True
    return False


# =========================
# 공용 수집기
# =========================
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
    page, home_url: str, max_items: int = 20, restrict_same_host: bool = True
) -> list[dict]:
    safe_goto(page, home_url, "home")
    try_close_common_popups(page)
    page.wait_for_timeout(2000)

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


# =========================
# 사이트별 스크레이퍼
# =========================
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


# ---- 하파크리스틴 ----
def _event_id_from_url(u: str) -> int:
    m = re.search(r"/events/(\d+)", u)
    return int(m.group(1)) if m else 0


def _hapakristin_try_open_ongoing(page) -> bool:
    """
    hover 후 하위 메뉴가 생기는 구조 대응:
    header/nav 내부에서 '이벤트' 텍스트 후보를 hover → '진행 중인 이벤트' 클릭
    """
    try:
        candidates = page.locator("header, nav").locator("a, button").filter(has_text=re.compile("이벤트"))
        n = min(candidates.count(), 6)
        if n == 0:
            return False

        for i in range(n):
            try:
                candidates.nth(i).hover(timeout=6_000)
                page.wait_for_timeout(600)
                sub = page.locator("text=진행 중인 이벤트")
                if sub.count() > 0:
                    sub.first.click(timeout=6_000)
                    page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue

        return False
    except Exception:
        return False


def _hapakristin_collect_header_event_ids(page) -> list[str]:
    """
    hover로 메뉴가 생성된 경우, header/nav 안의 /events/<id> 링크를 수집
    """
    hrefs = page.evaluate(
        """() => {
            const roots = Array.from(document.querySelectorAll('header, nav'));
            const out = [];
            for (const r of roots){
              for (const a of Array.from(r.querySelectorAll('a[href]'))){
                out.push({href: a.getAttribute('href') || '', text: (a.innerText || '').trim()});
              }
            }
            return out;
        }"""
    )
    base = page.url
    urls = []
    for row in hrefs:
        h = (row.get("href") or "").strip()
        t = (row.get("text") or "").strip()
        if not h:
            continue
        absu = _abs_url(base, h)
        if not absu or not _same_host(base, absu):
            continue
        if not re.search(r"^https://hapakristin\.co\.kr/events/\d+/?$", absu):
            continue
        if t in ("이벤트", "EVENT", ""):
            continue
        urls.append(absu.rstrip("/"))

    urls = list(dict.fromkeys(urls))
    urls.sort(key=_event_id_from_url, reverse=True)
    return urls[:10]


def scrape_hapakristin(page) -> list[dict]:
    safe_goto(page, "https://hapakristin.co.kr/", "hapakristin_home")
    try_close_common_popups(page)
    page.wait_for_timeout(1000)

    # 1) hover 성공 시: header/nav에서 진행중 목록 링크 수집
    if _hapakristin_try_open_ongoing(page):
        event_urls = _hapakristin_collect_header_event_ids(page)
        if event_urls:
            print("[hapakristin] collected ids:", [_event_id_from_url(u) for u in event_urls])
            results = [{"key": u, "url": u, "title": f"이벤트 {_event_id_from_url(u)}"} for u in event_urls]
            print("[hapakristin] events found:", len(results))
            return results

    # 2) hover 실패는 정상 fallback이므로 debug를 만들지 않음
    fixed = [
        "https://hapakristin.co.kr/events/6824",
        "https://hapakristin.co.kr/events/6724",
    ]

    # 고정 URL 자체가 진짜로 깨졌을 때만 debug 생성
    bad = []
    for u in fixed:
        st = safe_goto(page, u, f"hapakristin_check_{_event_id_from_url(u)}")

        # status를 못 받는(None) 케이스는 정상일 수 있으므로 실패로 보지 않음
        if st is not None and st >= 400:
            bad.append((u, st))
            continue

        # status가 None이어도, 페이지가 명백히 에러 페이지면 실패로 간주
        if st is None and page_looks_like_error(page):
            bad.append((u, "unknown_but_error_page"))

    if bad:
        _save_debug(page, "hapakristin_fixed_url_bad")
        _save_debug_text(
            "hapakristin_fixed_url_bad_info",
            "\n".join([f"{u} status={st}" for u, st in bad]),
        )

    print("[hapakristin] fallback fixed ids:", [_event_id_from_url(u) for u in fixed])
    results = [{"key": u, "url": u, "title": f"이벤트 {_event_id_from_url(u)}"} for u in fixed]
    print("[hapakristin] events found:", len(results))
    return results


# ---- Lens-me / i-sha / lenbling / yourly / i-dol ----
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


# ---- 메인 배너 기반 ----
def scrape_myfipn(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://www.myfipn.com/")


def scrape_chuulens(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://chuulens.kr/")


def scrape_gemhour(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://gemhour.co.kr/")


# ---- shop.winc.app ----
def scrape_shop_winc(page) -> list[dict]:
    home = "https://shop.winc.app/"
    safe_goto(page, home, "winc_home")
    page.wait_for_timeout(2000)

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


# ---- ann365 (contact_event 허브) ----
def _extract_codes_from_strings(strings: list[str]) -> list[str]:
    codes = []
    for s in strings:
        if not s:
            continue
        s = str(s)

        for m in re.findall(r"contact_event\.php\?code=([^&\"'\s]+)", s):
            if m and m != "$code":
                codes.append(m)

        for m in re.findall(r"[?&]code=([^&\"'\s]+)", s):
            if m and m != "$code":
                codes.append(m)

        for m in re.findall(r"code\s*[:=]\s*['\"]([^'\"]+)['\"]", s):
            if m and m != "$code":
                codes.append(m)

    out, seen = [], set()
    for c in codes:
        c = c.strip()
        if len(c) < 2:
            continue
        if c not in seen:
            out.append(c)
            seen.add(c)
    return out


def scrape_ann365(page) -> list[dict]:
    base_list = "https://ann365.com/contact/contact_event.php?code=$code&scategory=&pg="
    max_pages = 15
    max_items = 40

    results = []
    seen_codes = set()
    empty_streak = 0  # 연속 무수집 페이지 수

    for pg in range(1, max_pages + 1):
        list_url = f"{base_list}{pg}"
        safe_goto(page, list_url, "ann365_list")
        try_close_common_popups(page)
        page.wait_for_timeout(900)

        collected_strings = []
        try:
            collected_strings += page.evaluate(
                """() => {
                    const out = [];
                    const els = Array.from(document.querySelectorAll('a, button, [onclick], [data-href], [data-url], [data-code]'));
                    for (const el of els){
                      out.push(el.getAttribute('href') || '');
                      out.push(el.getAttribute('onclick') || '');
                      out.push(el.getAttribute('data-href') || '');
                      out.push(el.getAttribute('data-url') || '');
                      out.push(el.getAttribute('data-code') || '');
                    }
                    const scripts = Array.from(document.querySelectorAll('script'));
                    for (const s of scripts){
                      const t = s.innerText || '';
                      if (t && t.length < 200000) out.push(t);
                    }
                    return out;
                }"""
            )
        except Exception:
            pass

        codes_this_page = _extract_codes_from_strings(collected_strings)

        if not codes_this_page:
            empty_streak += 1
            if pg == 1:
                _save_debug(page, "ann365_no_codes_page1")
            if empty_streak >= 2:
                break
            continue
        else:
            empty_streak = 0

        new_added = 0
        for code in codes_this_page:
            if code in seen_codes:
                continue
            seen_codes.add(code)
            detail_url = f"https://ann365.com/contact/contact_event.php?code={code}&scategory=&pg="
            results.append({"key": f"ann365:code:{code}", "url": detail_url, "title": f"이벤트 {code}"})
            new_added += 1
            if len(results) >= max_items:
                break

        if new_added == 0:
            empty_streak += 1
            if empty_streak >= 2:
                break

        if len(results) >= max_items:
            break

    results = _dedup_keep_order(results)
    print("[ann365] events found:", len(results))
    if not results:
        _save_debug(page, "ann365_no_results")
    return results


# =========================
# 사이트 목록 (표시명 한글)
# =========================
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


# =========================
# 메인
# =========================
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
                    try:
                        post_slack_prod(msg)
                    except Exception as e:
                        print("[main] prod slack failed:", repr(e))
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
