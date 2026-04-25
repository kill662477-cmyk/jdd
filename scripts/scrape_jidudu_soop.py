import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

USER_ID = "wjswlgns09"
STATION_URL = f"https://www.sooplive.com/{USER_ID}"
LIVE_URL = f"https://play.sooplive.com/{USER_ID}"
VOD_URL = f"https://www.sooplive.com/station/{USER_ID}/vod/review"
CALENDAR_URL = f"https://www.sooplive.com/station/{USER_ID}/calendar"

ROOT = Path(__file__).resolve().parents[1]
DEBUG = ROOT / "debug"
DEBUG.mkdir(exist_ok=True)

KST = timezone(timedelta(hours=9))


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def safe_screenshot(page, name: str) -> None:
    try:
        await page.screenshot(path=str(DEBUG / f"{name}.png"), full_page=True)
        html = await page.content()
        (DEBUG / f"{name}.html").write_text(html, encoding="utf-8")
    except Exception as e:
        print(f"[debug-save-failed] {name}: {e}")


async def dismiss_popups(page) -> None:
    # SOOP/브라우저 환경에 따라 뜨는 팝업, 쿠키, 안내창 후보 닫기
    candidates = [
        "button:has-text('닫기')",
        "button:has-text('확인')",
        "button:has-text('오늘 하루 보지 않기')",
        "text=닫기",
        ".btn_close",
        ".close",
        "[aria-label='close']",
        "[aria-label='Close']",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=700)
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def goto(page, url: str, label: str) -> None:
    print(f"[goto] {label}: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    await dismiss_popups(page)


async def extract_latest_vod(page) -> Dict[str, Any]:
    await goto(page, VOD_URL, "vod")
    await safe_screenshot(page, "vod_page")

    # 1순위: 눈에 보이는 VOD 카드 링크 후보에서 추출
    link_candidates = [
        f"a[href*='/station/{USER_ID}/vod/']",
        "a[href*='/vod/']",
        "a[href*='vod']",
    ]

    for selector in link_candidates:
        links = await page.locator(selector).all()
        for link in links[:30]:
            try:
                href = await link.get_attribute("href")
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.sooplive.com" + href
                if "vod" not in href:
                    continue

                title = clean_text(await link.inner_text(timeout=1000))
                img = link.locator("img").first
                thumb = ""
                if await img.count():
                    thumb = (
                        await img.get_attribute("src")
                        or await img.get_attribute("data-src")
                        or await img.get_attribute("data-original")
                        or ""
                    )
                    if thumb.startswith("//"):
                        thumb = "https:" + thumb
                    elif thumb.startswith("/"):
                        thumb = "https://www.sooplive.com" + thumb

                # 링크 텍스트가 비어 있으면 주변 카드 텍스트를 보완
                if not title:
                    try:
                        card_text = await link.locator("xpath=ancestor::*[self::li or self::div][1]").inner_text(timeout=1000)
                        title = clean_text(card_text)
                    except Exception:
                        title = "최근 다시보기"

                return {
                    "title": title[:160] or "최근 다시보기",
                    "url": href,
                    "thumb": thumb,
                    "source": VOD_URL,
                    "scrapedAt": now_kst_iso(),
                }
            except Exception:
                continue

    # 2순위: 전체 HTML에서 이미지/링크 후보 fallback
    html = await page.content()
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "vod" not in href:
            continue
        if href.startswith("/"):
            href = "https://www.sooplive.com" + href
        img = a.find("img")
        thumb = ""
        if img:
            thumb = img.get("src") or img.get("data-src") or img.get("data-original") or ""
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            elif thumb.startswith("/"):
                thumb = "https://www.sooplive.com" + thumb
        title = clean_text(a.get_text(" ", strip=True)) or "최근 다시보기"
        return {
            "title": title[:160],
            "url": href,
            "thumb": thumb,
            "source": VOD_URL,
            "scrapedAt": now_kst_iso(),
        }

    return {
        "title": "다시보기를 불러오지 못했습니다",
        "url": VOD_URL,
        "thumb": "",
        "source": VOD_URL,
        "error": "NO_VOD_FOUND",
        "scrapedAt": now_kst_iso(),
    }


async def get_day_cells(page):
    """
    SOOP 캘린더 구조가 바뀔 수 있어서 여러 후보를 순서대로 시도.
    목표: 현재 보이는 주간 일/월/화/수/목/금/토 7칸.
    """
    candidates = [
        ".calendar_week li",
        ".calendar-week li",
        ".week_calendar li",
        ".weekly li",
        ".calendar_list li",
        "ul:has-text('일') li",
        "[class*='calendar'] li",
        "[class*='Calendar'] li",
        "button:has-text('일'), button:has-text('월'), button:has-text('화'), button:has-text('수'), button:has-text('목'), button:has-text('금'), button:has-text('토')",
    ]

    for sel in candidates:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            if count >= 7:
                print(f"[calendar] day selector matched: {sel}, count={count}")
                return [loc.nth(i) for i in range(min(count, 7))]
        except Exception:
            continue

    # 텍스트 기반 fallback: 일월화수목금토 포함한 클릭 가능한 요소
    text_candidates = []
    for day in ["일", "월", "화", "수", "목", "금", "토"]:
        loc = page.locator(f"text={day}").first
        try:
            if await loc.count():
                text_candidates.append(loc)
        except Exception:
            pass
    if len(text_candidates) >= 7:
        print("[calendar] text day fallback matched")
        return text_candidates[:7]

    return []


async def read_schedule_box_text(page) -> str:
    """
    날짜 클릭 후 아래 박스 텍스트를 읽는다.
    실제 선택자 모를 때를 대비해 후보를 넓게 둠.
    """
    candidates = [
        ".schedule",
        ".schedule_box",
        ".scheduleBox",
        ".calendar_detail",
        ".calendar-detail",
        ".detail_box",
        ".detailBox",
        ".cont_calendar",
        ".station_calendar",
        "[class*='schedule']",
        "[class*='Schedule']",
        "[class*='calendar'] [class*='detail']",
        "[class*='Calendar'] [class*='Detail']",
    ]

    best = ""
    for sel in candidates:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            for i in range(min(count, 5)):
                text = clean_text(await loc.nth(i).inner_text(timeout=1200))
                # 너무 짧은 텍스트/달력 전체 텍스트는 제외
                if len(text) > len(best) and len(text) >= 2:
                    best = text
        except Exception:
            continue

    if best:
        return best

    # fallback: body 전체에서 의미 있어 보이는 마지막 일부
    try:
        body = clean_text(await page.locator("body").inner_text(timeout=1500))
        return body[-500:] if body else ""
    except Exception:
        return ""


def parse_schedule_text(text: str) -> List[Dict[str, str]]:
    """
    SOOP 텍스트 구조가 일정하지 않아서 원문 기반으로 보관.
    시간이 잡히면 time 필드도 넣음.
    """
    text = clean_text(text)
    if not text:
        return []

    # 휴방/방송 없음 케이스
    if any(x in text for x in ["일정이 없습니다", "등록된 일정이 없습니다", "없습니다"]):
        return []

    # 너무 긴 달력 전체 텍스트를 줄인다
    chunks = re.split(r"(?=(?:오전|오후)\s*\d{1,2}|(?:\d{1,2}:\d{2}))", text)
    items = []
    for chunk in chunks:
        chunk = clean_text(chunk)
        if not chunk:
            continue
        if len(chunk) < 2:
            continue
        time_match = re.search(r"(오전|오후)\s*\d{1,2}(?::\d{2})?|(\d{1,2}:\d{2})", chunk)
        items.append({
            "time": time_match.group(0) if time_match else "",
            "title": chunk[:180],
            "raw": chunk,
        })

    if not items:
        items.append({"time": "", "title": text[:180], "raw": text})

    return items[:5]


async def extract_week_schedule(page) -> Dict[str, Any]:
    await goto(page, CALENDAR_URL, "calendar")
    await safe_screenshot(page, "calendar_page_initial")

    day_names = ["일", "월", "화", "수", "목", "금", "토"]
    day_cells = await get_day_cells(page)

    week_items: List[Dict[str, Any]] = []

    if not day_cells:
        await safe_screenshot(page, "calendar_no_day_cells")
        return {
            "items": [],
            "source": CALENDAR_URL,
            "error": "NO_DAY_CELLS_FOUND",
            "scrapedAt": now_kst_iso(),
        }

    for idx, cell in enumerate(day_cells[:7]):
        day_name = day_names[idx]
        try:
            before = ""
            try:
                before = clean_text(await page.locator("body").inner_text(timeout=1000))[:300]
            except Exception:
                pass

            await cell.scroll_into_view_if_needed(timeout=3000)
            await cell.click(timeout=5000)
            await page.wait_for_timeout(900)
            await dismiss_popups(page)

            # 클릭 후 DOM 변경/박스 로딩 대기 약간
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass

            text = await read_schedule_box_text(page)
            parsed = parse_schedule_text(text)
            week_items.append({
                "dayIndex": idx,
                "dayName": day_name,
                "text": text,
                "items": parsed,
            })
            print(f"[calendar] {day_name}: {text[:100]}")
        except Exception as e:
            week_items.append({
                "dayIndex": idx,
                "dayName": day_name,
                "text": "",
                "items": [],
                "error": str(e),
            })
            await safe_screenshot(page, f"calendar_day_{idx}_error")

    await safe_screenshot(page, "calendar_page_done")
    return {
        "items": week_items,
        "source": CALENDAR_URL,
        "scrapedAt": now_kst_iso(),
    }


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1365, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        vod = await extract_latest_vod(page)
        save_json(ROOT / "vod.json", {"latest": vod, "scrapedAt": now_kst_iso()})
        print("[saved] vod.json")

        schedule = await extract_week_schedule(page)
        save_json(ROOT / "schedule.json", schedule)
        print("[saved] schedule.json")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
