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

# 첫 도입 시 기존 이벤트/배너가 한꺼번에 알림 되는 것을 막고 싶으면 True 권장
INIT_SILENT = True

# 각 사이트에서 “너무 많이” 긁어오는 걸 방지하기 위한 상한
DEFAULT_MAX_ITEMS = 30


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
# 범용 스크레이퍼(보드/메뉴/배너)
# -----------------------------

def scrape_list_page_anchors(
    page,
    list_url: str,
    include_patterns: list[str],
    exclude_patterns: list[str] | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[dict]:
    """
    서버/SPA 무관하게, 목록 페이지에서 a[href]를 수집하여 include_patterns에 맞는 링크를 반환합니다.
    """
    exclude_patterns = exclude_patterns or []
    print("[list] goto", list_url)
    page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)

    anchors = page.locator("a[href]").all()
    base = page.url
    results = []

    for a in anchors:
        href = a.get_attribute("href") or ""
        url = _abs_url(base, href)
        if not url:
            continue

        # include
        if include_patterns and not any(re.search(p, url) for p in include_patterns):
            continue
        # exclude
        if exclude_patterns and any(re.search(p, url) for p in exclude_patterns):
            continue

        title = (a.inner_text() or "").strip()
        if not title:
            # 이미지 링크일 수 있어 alt로 보강
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
) -> tuple[str, list[dict]]:
    """
    홈으로 간 뒤, GNB(또는 어딘가)의 menu_text를 포함한 a/button을 클릭해서 페이지 이동 후,
    그 페이지에서 include_patterns 링크를 수집합니다.
    반환: (이동한 url, 수집 결과)
    """
    print("[gnb] goto", home_url)
    page.goto(home_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)

    # 클릭 후보(사이트마다 메뉴 태그가 다름)
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
                loc.first.click(timeout=8000)
                clicked = True
                # 라우팅/로드 대기
                try:
                    page.wait_for_timeout(800)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                # URL이 안 바뀌는 SPA도 있어 추가 대기
                page.wait_for_timeout(2000)
                after = page.url
                print("[gnb] clicked menu, url:", before, "->", after)
                break
            except Exception:
                pass

    if not clicked:
        _save_debug(page, f"gnb_click_fail_{menu_text}")
        return (page.url, [])

    # 이동한 페이지에서 링크 수집
    moved_url = page.url
    results = scrape_list_page_anchors(
        page,
        moved_url,
        include_patterns=include_patterns,
        exclude_patterns=[],
        max_items=max_items,
    )
    return (moved_url, results)


def scrape_main_banners_by_image_links(
    page,
    home_url: str,
    max_items: int = 20,
    restrict_same_host: bool = True,
) -> list[dict]:
    """
    메인 배너는 대개 이미지가 포함된 링크로 구성됩니다.
    a:has(img)를 폭넓게 수집한 뒤, 중복 제거로 관리합니다.
    """
    print("[banner] goto", home_url)
    page.goto(home_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)

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
    """
    O-Lens: 카드 클릭형(기존 동작 유지)
    """
    EVENT_LIST_URL = "https://o-lens.com/event/list"
    print("[olens] goto", EVENT_LIST_URL)
    page.goto(EVENT_LIST_URL, wait_until="domcontentloaded", timeout=60000)

    cards = page.locator("div.board-information__wrapper")
    try:
        cards.first.wait_for(timeout=25000)
    except PlaywrightTimeoutError:
        _save_debug(page, "olens_list_timeout")
        return []

    total = cards.count()
    n = min(total, 20)
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
                    page.goto(EVENT_LIST_URL, wait_until="domcontentloaded", timeout=60000)
                    page.locator("div.board-information__wrapper").first.wait_for(timeout=25000)
                    cards = page.locator("div.board-information__wrapper")

        if url:
            results.append({"key": url, "url": url, "title": title})

    return _dedup_keep_order(results)


def scrape_lensme(page) -> list[dict]:
    """
    Lens-me: 이벤트 보드
    """
    url = "https://www.lens-me.com/shop/board.php?ps_bbscuid=17"
    return scrape_list_page_anchors(
        page,
        list_url=url,
        include_patterns=[r"ps_mode=view", r"ps_uid=\d+"],
        exclude_patterns=[],
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_hapakristin(page) -> list[dict]:
    """
    Hapa Kristin: GNB의 '진행 중인 이벤트' 하위에 프로모션 추가 구조
    - 메뉴 클릭 후, collections/pages/products 링크를 수집
    """
    home = "https://hapakristin.co.kr/"
    _, results = scrape_gnb_click_then_collect(
        page,
        home_url=home,
        menu_text="진행 중인 이벤트",
        include_patterns=[r"hapakristin\.co\.kr/(collections|pages|products)/"],
        max_items=DEFAULT_MAX_ITEMS,
    )
    return results


def scrape_myfipn(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://www.myfipn.com/", max_items=20, restrict_same_host=True)


def scrape_chuulens(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://chuulens.kr/", max_items=20, restrict_same_host=True)


def scrape_gemhour(page) -> list[dict]:
    return scrape_main_banners_by_image_links(page, "https://gemhour.co.kr/", max_items=20, restrict_same_host=True)


def scrape_i_sha(page) -> list[dict]:
    """
    i-sha: 이벤트 보드(워드프레스/게시판 형태)
    """
    url = "https://i-sha.kr/board/%EC%9D%B4%EB%B2%A4%ED%8A%B8/8/"
    # 보통 /?p=, /board/이벤트/, /event/ 등으로 상세가 열림. 우선 'board' 하위 링크를 넓게 수집
    return scrape_list_page_anchors(
        page,
        list_url=url,
        include_patterns=[r"i-sha\.kr/board/"],
        exclude_patterns=[r"/page/\d+/?$"],  # 페이징 제외
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_lenbling(page) -> list[dict]:
    """
    lenbling: /board/event/8/ 목록 + 하위 상세
    """
    url = "https://lenbling.com/board/event/8/"
    return scrape_list_page_anchors(
        page,
        list_url=url,
        include_patterns=[r"lenbling\.com/board/event/"],
        exclude_patterns=[r"/board/event/8/?$"],  # 자기 자신 제외
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_yourly(page) -> list[dict]:
    """
    yourly: /board/event 목록 + 하위 상세
    """
    url = "https://yourly.kr/board/event"
    return scrape_list_page_anchors(
        page,
        list_url=url,
        include_patterns=[r"yourly\.kr/board/event"],
        exclude_patterns=[r"/board/event/?$"],  # 목록 자신 제외
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_i_dol(page) -> list[dict]:
    """
    i-dol: 이벤트 페이지 존재
    """
    url = "https://www.i-dol.kr/bbs/event1.php"
    return scrape_list_page_anchors(
        page,
        list_url=url,
        include_patterns=[r"i-dol\.kr/bbs/"],
        exclude_patterns=[r"event1\.php$"],  # 목록 자신 제외(상세가 같은 파일일 수도 있어, 필요 시 제거)
        max_items=DEFAULT_MAX_ITEMS,
    )


def scrape_shop_winc(page) -> list[dict]:
    """
    shop.winc.app: URL을 모르지만 GNB에 '이벤트' 메뉴가 항상 있음
    - 홈에서 '이벤트' 클릭 후, 이동한 페이지에서 이벤트/프로모션 후보 링크 수집
    """
    home = "https://shop.winc.app/"
    moved_url, results = scrape_gnb_click_then_collect(
        page,
        home_url=home,
        menu_text="이벤트",
        include_patterns=[
            r"shop\.winc\.app/.*event",
            r"shop\.winc\.app/.*promotion",
            r"shop\.winc\.app/board",
            r"shop\.winc\.app/bbs",
        ],
        max_items=DEFAULT_MAX_ITEMS,
    )

    # moved_url 자체가 이벤트 허브면, 그 페이지 URL을 key로 저장하는 것도 방법
    # (이벤트 목록 구조가 링크를 거의 안 주는 경우 대비)
    if not results:
        results = [{"key": moved_url, "url": moved_url, "title": "이벤트 페이지(메뉴 이동)"}]

    return results


def scrape_ann365(page) -> list[dict]:
    """
    ann365: SALE 하위에 이벤트 링크가 생김
    - 홈(또는 메뉴 페이지)에서 'SALE' 클릭 -> 그 안에서 '이벤트' 혹은 event 포함 링크 수집
    """
    base = "https://ann365.com/sub/menu.php"
    print("[ann365] goto", base)
    page.goto(base, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)

    # 1) SALE 클릭(가능하면)
    clicked_sale = False
    for sel in [f'a:has-text("SALE")', f'button:has-text("SALE")', f'div:has-text("SALE")']:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                loc.first.click(timeout=8000)
                clicked_sale = True
                page.wait_for_timeout(2000)
                break
            except Exception:
                pass

    if not clicked_sale:
        # SALE 클릭이 실패해도 현재 페이지 내 링크에서 'event'를 찾아본다
        print("[ann365] SALE click failed (continue)")

    # 2) SALE 하위에서 이벤트 링크 수집
    # ann365는 메뉴 구조상 /sub/menu.php?menu=... 처럼 뜰 가능성이 높아 넓게 잡는다
    return scrape_list_page_anchors(
        page,
        list_url=page.url,
        include_patterns=[r"ann365\.com/.*(event|이벤트|menu\.php)"],
        exclude_patterns=[],
        max_items=DEFAULT_MAX_ITEMS,
    )


# -----------------------------
# 러너
# -----------------------------

SITES = [
    {"site": "O-Lens", "fn": scrape_olens},
    {"site": "Hapa Kristin", "fn": scrape_hapakristin},
    {"site": "Lens-me", "fn": scrape_lensme},
    {"site": "MYFiPN", "fn": scrape_myfipn},
    {"site": "CHUU Lens", "fn": scrape_chuulens},
    {"site": "Gemhour", "fn": scrape_gemhour},
    {"site": "i-sha", "fn": scrape_i_sha},
    {"site": "shop.winc.app", "fn": scrape_shop_winc},
    {"site": "ann365", "fn": scrape_ann365},
    {"site": "lenbling", "fn": scrape_lenbling},
    {"site": "yourly", "fn": scrape_yourly},
    {"site": "i-dol", "fn": scrape_i_dol},
]

def main():
    state = load_state()
    if "seen" not in state:
        state["seen"] = {}

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

            print("\n[main] site:", site)
            context = _new_context(browser)
            page = context.new_page()
            page.set_default_timeout(30000)

            try:
                items = fn(page) or []
            except Exception as e:
                print("[main] error in site:", site, repr(e))
                _save_debug(page, f"error_{site.replace(' ', '_')}")
                items = []
            finally:
                try:
                    context.close()
                except Exception:
                    pass

            items = _dedup_keep_order(items)
            print("[main] scraped:", len(items))

            seen_set = set(state["seen"].get(site, []))

            # 첫 도입 시 조용히 초기화(알림 안 보내고 state만 채움)
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
