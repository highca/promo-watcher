# scraper/main.py
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import glob
import hashlib
import datetime
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Set

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


# ----------------------------
# 설정
# ----------------------------

STATE_DIR = "state"
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")
DEBUG_NOTIFIED_FILE = os.path.join(STATE_DIR, "debug_notified.json")
DEBUG_DIR = "debug"

OPS_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()         # 운영(신규 알림)
TEST_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL_TEST", "").strip()   # 테스트(경고 알림)

RUN_URL = os.environ.get("GITHUB_RUN_URL", "").strip()  # optional, workflow에서 env로 넘기면 좋음

DEFAULT_TIMEOUT_MS = 25_000
NAV_TIMEOUT_MS = 35_000

# “경고로 볼지” 판단을 더 엄격하게(=불필요 경고 줄이기)
# - 아래 조건을 만족하면 debug를 만들어도 경고를 보내지 않음
HAPAKRISTIN_FIXED_EVENT_IDS = [6824, 6724]


# ----------------------------
# 유틸
# ----------------------------

def ensure_dirs():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DEBUG_DIR, exist_ok=True)

def now_kst_str() -> str:
    # Actions는 UTC 기반이지만 표시는 KST로
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(tz=kst).strftime("%Y-%m-%d %H:%M:%S KST")

def ts_tag() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def stable_id(site_key: str, url: str, title: str = "") -> str:
    s = f"{site_key}::{url}::{title}".encode("utf-8")
    return hashlib.sha1(s).hexdigest()[:16]

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def post_slack(webhook: str, text: str):
    if not webhook:
        print("[slack] webhook not set, skip")
        return
    try:
        resp = requests.post(webhook, json={"text": text}, timeout=15)
        if resp.status_code >= 400:
            print(f"[slack] failed {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[slack] exception: {e}")

def list_debug_files() -> List[str]:
    files = []
    if os.path.isdir(DEBUG_DIR):
        for p in glob.glob(os.path.join(DEBUG_DIR, "*")):
            if os.path.isfile(p):
                files.append(os.path.basename(p))
    return sorted(files)

def save_debug_text(name_prefix: str, content: str) -> str:
    fn = f"{name_prefix}_{ts_tag()}.txt"
    path = os.path.join(DEBUG_DIR, fn)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[debug] saved {path}")
    return fn

def save_debug_html_png(page, name_prefix: str) -> Tuple[str, str]:
    html_fn = f"{name_prefix}_{ts_tag()}.html"
    png_fn = f"{name_prefix}_{ts_tag()}.png"

    html_path = os.path.join(DEBUG_DIR, html_fn)
    png_path = os.path.join(DEBUG_DIR, png_fn)

    try:
        html = page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        page.screenshot(path=png_path, full_page=True)
        print(f"[debug] saved {png_path} {html_path}")
    except Exception as e:
        # 최소한 텍스트라도 남김
        save_debug_text(name_prefix + "_exception", str(e))
        return ("", "")

    return (html_fn, png_fn)

def safe_goto(page, url: str, label: str):
    print(f"[goto][{label}] {url}")
    return page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def abs_url(base: str, href: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href


# ----------------------------
# 데이터 모델
# ----------------------------

@dataclass
class Item:
    site_key: str
    site_name: str
    title: str
    url: str
    item_id: str

def make_item(site_key: str, site_name: str, title: str, url: str) -> Item:
    return Item(
        site_key=site_key,
        site_name=site_name,
        title=norm_text(title) if title else "(제목 미확인)",
        url=url,
        item_id=stable_id(site_key, url, title or "")
    )


# ----------------------------
# 수집기(사이트별)
# ----------------------------

def scrape_olens(page) -> List[Item]:
    site_key = "olens"
    site_name = "오렌즈"
    url = "https://o-lens.com/event/list"

    safe_goto(page, url, "olens_list")
    page.wait_for_timeout(1200)

    items: List[Item] = []
    # 카드/리스트 내 링크 수집
    anchors = page.query_selector_all("a[href]")
    for a in anchors:
        href = a.get_attribute("href") or ""
        full = abs_url("https://o-lens.com", href)
        if "/event/" not in full and "/event" not in full:
            continue
        t = a.inner_text() or ""
        t = norm_text(t)
        if not t:
            continue
        items.append(make_item(site_key, site_name, t, full))

    # 중복 제거
    uniq = {}
    for it in items:
        uniq[it.item_id] = it
    return list(uniq.values())

def scrape_list_page(page, site_key: str, site_name: str, list_url: str, base: str, allow_patterns: List[str]) -> List[Item]:
    safe_goto(page, list_url, "list")
    page.wait_for_timeout(1000)

    anchors = page.query_selector_all("a[href]")
    items: List[Item] = []

    for a in anchors:
        href = a.get_attribute("href") or ""
        full = abs_url(base, href)
        if not full:
            continue
        ok = any((re.search(p, full) is not None) for p in allow_patterns)
        if not ok:
            continue

        title = a.inner_text() or ""
        title = norm_text(title)
        if not title:
            # 이미지 링크인 경우 aria-label/alt 일부 추출 시도
            aria = a.get_attribute("aria-label") or ""
            title = norm_text(aria)
        if not title:
            continue

        items.append(make_item(site_key, site_name, title, full))

    uniq = {}
    for it in items:
        uniq[it.item_id] = it
    print(f"[list] found: {len(uniq)}")
    return list(uniq.values())

def scrape_banner(page, site_key: str, site_name: str, home_url: str, base: str, allow_patterns: List[str]) -> List[Item]:
    safe_goto(page, home_url, "banner")
    page.wait_for_timeout(1500)

    items: List[Item] = []
    # 배너는 보통 a 또는 swiper-slide 내부 a
    anchors = page.query_selector_all("a[href]")
    for a in anchors:
        href = a.get_attribute("href") or ""
        full = abs_url(base, href)
        if not full:
            continue
        ok = any((re.search(p, full) is not None) for p in allow_patterns)
        if not ok:
            continue

        # 배너는 텍스트가 없을 수 있으니 alt/aria-label 우선
        title = a.get_attribute("aria-label") or ""
        if not title:
            img = a.query_selector("img[alt]")
            if img:
                title = img.get_attribute("alt") or ""
        if not title:
            title = a.inner_text() or ""
        title = norm_text(title) or "(배너)"

        items.append(make_item(site_key, site_name, title, full))

    uniq = {}
    for it in items:
        uniq[it.item_id] = it
    print(f"[banner] found: {len(uniq)}")
    return list(uniq.values())

def hapakristin_event_page_looks_ok(page) -> bool:
    """
    하파크리스틴은 Playwright에서 status가 404로 찍혀도 SPA 쉘이 로드되는 경우가 있습니다.
    따라서 status만으로 실패 처리하지 않고, 페이지 정황을 보고 “정상 로드”를 판단합니다.
    """
    try:
        title = page.title() or ""
        url = page.url or ""
        html = page.content() or ""
    except Exception:
        return False

    title_ok = ("이벤트 페이지" in title) or ("Hapa Kristin" in title)
    url_ok = ("/events/" in url)
    app_ok = ('id="app"' in html) or ('id="app"' in html.lower())
    # 이벤트 페이지는 보통 /events/<id>로 유지되고, app root가 존재
    return (title_ok and url_ok and app_ok)

def scrape_hapakristin(page) -> Tuple[List[Item], bool]:
    """
    반환: (items, had_hard_failure)
    - 하드 실패: 고정 URL도 못 모으거나(0개), 페이지 로딩이 아예 깨진 경우
    - 메뉴 탐색 실패는 fallback이 성공하면 하드 실패로 보지 않음(=경고 억제)
    """
    site_key = "hapakristin"
    site_name = "하파크리스틴"
    home = "https://hapakristin.co.kr/"

    safe_goto(page, home, "hapakristin_home")
    page.wait_for_timeout(1500)

    # 1) 우선 고정 URL 2개를 기준으로 “진짜 진행중 이벤트”를 확보
    fixed_ids = HAPAKRISTIN_FIXED_EVENT_IDS[:]
    fixed_urls = [f"https://hapakristin.co.kr/events/{i}" for i in fixed_ids]

    fixed_ok = True
    fixed_items: List[Item] = []
    # 이벤트 페이지 접근은 “상태코드”가 아니라 “페이지 정황”으로 OK 판단
    for i, u in zip(fixed_ids, fixed_urls):
        resp = safe_goto(page, u, f"hapakristin_check_{i}")
        page.wait_for_timeout(1000)
        if not hapakristin_event_page_looks_ok(page):
            fixed_ok = False

        # 제목은 페이지 내부에서 안정적으로 뽑기 어려울 수 있어, ID 기반 타이틀로
        fixed_items.append(make_item(site_key, site_name, f"이벤트 {i}", u))

    if fixed_ok:
        # 고정 URL 기준 수집 성공이면, 불필요한 debug를 만들지 않고 바로 반환
        print(f"[hapakristin] fallback fixed ids: {fixed_ids}")
        print(f"[hapakristin] events found: {len(fixed_items)}")
        return (fixed_items, False)

    # 2) 고정 URL이 “정황상 실패”로 보이면, 그때만 추가 진단/디버그
    #    (이 경우에만 debug 생성 + 테스트 채널 경고 대상)
    html_fn, png_fn = save_debug_html_png(page, "hapakristin_fixed_url_bad")
    info = []
    for u in fixed_urls:
        try:
            r = page.goto(u, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            status = r.status if r else None
            info.append(f"{u} status={status}")
        except Exception as e:
            info.append(f"{u} exception={e}")
    info_fn = save_debug_text("hapakristin_fixed_url_bad_info", "\n".join(info))

    print(f"[hapakristin] fallback fixed ids: {fixed_ids}")
    print(f"[hapakristin] events found: {len(fixed_items)}")

    # 고정 URL 자체는 아이템으로 반환(신규 감지 목적), 다만 하드 실패로 플래그
    # - 고정 2개라도 반환되므로 신규 알림은 정상 동작
    # - 대신 경고는 “새 debug 생성” 기준으로만 1회 발송
    return (fixed_items, True)

def scrape_lensme(page) -> List[Item]:
    return scrape_list_page(
        page=page,
        site_key="lensme",
        site_name="렌즈미",
        list_url="https://www.lens-me.com/shop/board.php?ps_bbscuid=17",
        base="https://www.lens-me.com",
        allow_patterns=[r"/shop/board\.php\?ps_bbscuid=17", r"/shop/board\.php\?ps_bbspuid="],
    )

def scrape_myfipn(page) -> List[Item]:
    return scrape_banner(
        page=page,
        site_key="myfipn",
        site_name="마이핍앤",
        home_url="https://www.myfipn.com/",
        base="https://www.myfipn.com",
        allow_patterns=[r"/event", r"/promotion", r"/board", r"/pages", r"/collections", r"/product", r"/products"],
    )

def scrape_chuulens(page) -> List[Item]:
    return scrape_banner(
        page=page,
        site_key="chuulens",
        site_name="츄렌즈",
        home_url="https://chuulens.kr/",
        base="https://chuulens.kr",
        allow_patterns=[r"/event", r"/promotion", r"/board", r"/product", r"/products"],
    )

def scrape_gemhour(page) -> List[Item]:
    return scrape_banner(
        page=page,
        site_key="gemhour",
        site_name="젬아워",
        home_url="https://gemhour.co.kr/",
        base="https://gemhour.co.kr",
        allow_patterns=[r"/event", r"/promotion", r"/board", r"/product", r"/products"],
    )

def scrape_isha(page) -> List[Item]:
    return scrape_list_page(
        page=page,
        site_key="isha",
        site_name="아이샤",
        list_url="https://i-sha.kr/board/%EC%9D%B4%EB%B2%A4%ED%8A%B8/8/",
        base="https://i-sha.kr",
        allow_patterns=[r"/board/", r"/article/", r"/product/"],
    )

def scrape_lenbling(page) -> List[Item]:
    return scrape_list_page(
        page=page,
        site_key="lenbling",
        site_name="렌블링",
        list_url="https://lenbling.com/board/event/8/",
        base="https://lenbling.com",
        allow_patterns=[r"/board/event/", r"/article/", r"/product/"],
    )

def scrape_yourly(page) -> List[Item]:
    return scrape_list_page(
        page=page,
        site_key="yourly",
        site_name="유어리",
        list_url="https://yourly.kr/board/event",
        base="https://yourly.kr",
        allow_patterns=[r"/board/event", r"/article/", r"/product/"],
    )

def scrape_idol(page) -> List[Item]:
    # i-dol -> 아이돌렌즈
    return scrape_list_page(
        page=page,
        site_key="idol",
        site_name="아이돌렌즈",
        list_url="https://www.i-dol.kr/bbs/event1.php",
        base="https://www.i-dol.kr",
        allow_patterns=[r"/bbs/event", r"/bbs/board", r"/shop/item", r"/product"],
    )

def scrape_ann365(page) -> Tuple[List[Item], bool]:
    """
    ann365: 이벤트 모음 페이지
    - code는 알 수 없으니, 리스트에서 실제 event 링크를 수집(상대/절대 모두)
    - 여러 페이지 순회하되, 연속 empty 페이지가 나오면 중단
    반환: (items, had_hard_failure)
    """
    site_key = "ann365"
    site_name = "앤365"
    base = "https://ann365.com"
    list_tpl = "https://ann365.com/contact/contact_event.php?code=$code&scategory=&pg={pg}"

    items: List[Item] = []
    empty_streak = 0
    max_pages = 20  # 안전장치

    for pg in range(1, max_pages + 1):
        url = list_tpl.format(pg=pg)
        safe_goto(page, url, "ann365_list")
        page.wait_for_timeout(900)

        # 링크 수집(이벤트 상세는 code 파라미터 또는 별도 링크일 수 있음)
        anchors = page.query_selector_all("a[href]")
        found_this_page = 0

        for a in anchors:
            href = a.get_attribute("href") or ""
            full = abs_url(base, href)

            # 이벤트/프로모션 관련 링크만(너무 넓히면 잡음이 많아짐)
            if not full:
                continue
            if ("contact_event" not in full) and ("event" not in full):
                continue

            title = a.inner_text() or ""
            title = norm_text(title)
            if not title:
                continue

            it = make_item(site_key, site_name, title, full)
            items.append(it)
            found_this_page += 1

        if found_this_page == 0:
            empty_streak += 1
            # 2페이지 연속으로 비면 끝으로 간주
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0

    # 중복 제거
    uniq = {}
    for it in items:
        uniq[it.item_id] = it
    items = list(uniq.values())

    if len(items) == 0:
        # 진짜로 못 긁은 경우에만 debug + 하드 실패
        html_fn, png_fn = save_debug_html_png(page, "ann365_no_results")
        return (items, True)

    print(f"[ann365] events found: {len(items)}")
    return (items, False)


# ----------------------------
# 메인 실행
# ----------------------------

def format_new_items_message(new_items: List[Item]) -> str:
    # 운영 채널: 신규만
    # 사이트별로 묶어서 보기 좋게
    by_site: Dict[str, List[Item]] = {}
    for it in new_items:
        by_site.setdefault(it.site_name, []).append(it)

    lines = []
    lines.append(f"[신규 프로모션 감지] {now_kst_str()}")
    if RUN_URL:
        lines.append(f"Run: {RUN_URL}")

    for site_name, items in by_site.items():
        lines.append("")
        lines.append(f"- {site_name} ({len(items)}건)")
        for it in items[:20]:
            # 너무 길면 슬랙 가독성 저하
            title = it.title
            if len(title) > 80:
                title = title[:77] + "..."
            lines.append(f"  • {title} | {it.url}")

        if len(items) > 20:
            lines.append(f"  … 외 {len(items)-20}건")

    return "\n".join(lines)

def format_debug_warning(site_name: str, new_debug_files: List[str]) -> str:
    lines = []
    lines.append(f"[수집 경고] {site_name}")
    lines.append("debug 파일이 새로 생성되었습니다(수집 실패/구조 변경 가능).")
    lines.append("새 debug: " + ", ".join(new_debug_files[:15]))
    if RUN_URL:
        lines.append(f"Run: {RUN_URL}")
    return "\n".join(lines)

def main():
    ensure_dirs()

    seen = load_json(SEEN_FILE, {})
    # 구조: { site_key: { item_id: {title,url,first_seen} } }
    if not isinstance(seen, dict):
        seen = {}

    debug_notified: Set[str] = set(load_json(DEBUG_NOTIFIED_FILE, []))
    if not isinstance(debug_notified, set):
        debug_notified = set(debug_notified) if isinstance(debug_notified, list) else set()

    debug_before = set(list_debug_files())

    new_items_all: List[Item] = []
    had_any_state_change = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
        )
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        sites = [
            ("olens", "오렌즈", lambda: scrape_olens(page)),
            ("hapakristin", "하파크리스틴", None),  # 특별 처리
            ("lensme", "렌즈미", lambda: scrape_lensme(page)),
            ("myfipn", "마이핍앤", lambda: scrape_myfipn(page)),
            ("chuulens", "츄렌즈", lambda: scrape_chuulens(page)),
            ("gemhour", "젬아워", lambda: scrape_gemhour(page)),
            ("isha", "아이샤", lambda: scrape_isha(page)),
            ("lenbling", "렌블링", lambda: scrape_lenbling(page)),
            ("yourly", "유어리", lambda: scrape_yourly(page)),
            ("idol", "아이돌렌즈", lambda: scrape_idol(page)),
            ("ann365", "앤365", None),  # 특별 처리
        ]

        for site_key, site_name, fn in sites:
            print(f"[main] site: {site_name} ( {site_name} )")

            t0 = time.time()
            items: List[Item] = []
            hard_fail = False

            try:
                if site_key == "hapakristin":
                    items, hard_fail = scrape_hapakristin(page)
                elif site_key == "ann365":
                    items, hard_fail = scrape_ann365(page)
                else:
                    items = fn() if fn else []
            except PWTimeoutError as e:
                hard_fail = True
                save_debug_text(f"{site_key}_timeout", str(e))
            except Exception as e:
                hard_fail = True
                save_debug_text(f"{site_key}_exception", repr(e))

            elapsed = time.time() - t0
            print(f"[main] scraped: {len(items)} elapsed={elapsed:.1f}s")

            # 신규 감지
            site_seen = seen.get(site_key, {})
            if not isinstance(site_seen, dict):
                site_seen = {}

            new_this_site: List[Item] = []
            for it in items:
                if it.item_id not in site_seen:
                    new_this_site.append(it)
                    site_seen[it.item_id] = {
                        "title": it.title,
                        "url": it.url,
                        "first_seen": now_kst_str(),
                    }

            if new_this_site:
                had_any_state_change = True
                new_items_all.extend(new_this_site)

            seen[site_key] = site_seen
            print(f"[main] new: {len(new_this_site)}")

            # “하드 실패”만으로는 곧장 경고하지 않음.
            # 경고는 “debug 파일이 새로 생성된 경우에만” 보내고,
            # 그마저도 이미 보낸 debug 파일은 재전송하지 않음.
            # (아래에서 일괄 처리)

        try:
            context.close()
        finally:
            browser.close()

    # 상태 저장
    save_json(SEEN_FILE, seen)

    # 운영 채널: 신규만 알림
    if new_items_all:
        msg = format_new_items_message(new_items_all)
        post_slack(OPS_WEBHOOK, msg)

    # debug 경고: “새로 생성된 debug 파일”만 + “미통지 파일”만
    debug_after = set(list_debug_files())
    created = sorted(list(debug_after - debug_before))
    created_unnotified = [f for f in created if f not in debug_notified]

    # 사이트별로 debug 파일을 대충 매핑(파일명 prefix로)
    # 예: hapakristin_*, ann365_*, list_no_results_* 등
    if created_unnotified and TEST_WEBHOOK:
        buckets: Dict[str, List[str]] = {}
        for fn in created_unnotified:
            low = fn.lower()
            if low.startswith("hapakristin_"):
                buckets.setdefault("하파크리스틴", []).append(fn)
            elif low.startswith("ann365_"):
                buckets.setdefault("앤365", []).append(fn)
            elif low.startswith("olens_"):
                buckets.setdefault("오렌즈", []).append(fn)
            elif low.startswith("lensme_"):
                buckets.setdefault("렌즈미", []).append(fn)
            elif low.startswith("myfipn_"):
                buckets.setdefault("마이핍앤", []).append(fn)
            elif low.startswith("chuulens_"):
                buckets.setdefault("츄렌즈", []).append(fn)
            elif low.startswith("gemhour_"):
                buckets.setdefault("젬아워", []).append(fn)
            elif low.startswith("isha_"):
                buckets.setdefault("아이샤", []).append(fn)
            elif low.startswith("lenbling_"):
                buckets.setdefault("렌블링", []).append(fn)
            elif low.startswith("yourly_"):
                buckets.setdefault("유어리", []).append(fn)
            elif low.startswith("idol_"):
                buckets.setdefault("아이돌렌즈", []).append(fn)
            else:
                buckets.setdefault("기타", []).append(fn)

        for site_name, files in buckets.items():
            post_slack(TEST_WEBHOOK, format_debug_warning(site_name, files))

        # 통지 기록 업데이트
        for fn in created_unnotified:
            debug_notified.add(fn)
        save_json(DEBUG_NOTIFIED_FILE, sorted(list(debug_notified)))

    print("[main] done")

if __name__ == "__main__":
    main()
