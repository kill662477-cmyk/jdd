import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

USER_ID = "wjswlgns09"
VOD_URL = f"https://www.sooplive.com/station/{USER_ID}/vod/review"
CALENDAR_URL = f"https://www.sooplive.com/station/{USER_ID}/calendar"

ROOT = Path(__file__).resolve().parents[1]
DEBUG = ROOT / "debug"
DEBUG.mkdir(exist_ok=True)

KST = timezone(timedelta(hours=9))


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def debug_save(page, name: str) -> None:
    try:
        await page.screenshot(path=str(DEBUG / f"{name}.png"), full_page=True)
        (DEBUG / f"{name}.html").write_text(await page.content(), encoding="utf-8")
    except Exception as e:
        print(f"[debug-save-error] {name}: {e}")


async def dismiss_popups(page) -> None:
    for sel in [
        "button:has-text('닫기')",
        "button:has-text('확인')",
        "button:has-text('오늘 하루 보지 않기')",
        "text=닫기",
        ".btn_close",
        ".close",
        "[aria-label='close']",
        "[aria-label='Close']",
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=600)
                await page.wait_for_timeout(250)
        except Exception:
            pass


async def goto(page, url: str, label: str) -> None:
    print(f"[goto] {label}: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3500)
    await dismiss_popups(page)


def is_bad_vod(title: str, url: str, thumb: str) -> bool:
    t = (title or "").strip()
    u = (url or "").lower()
    th = (thumb or "").lower()

    if not t:
        return True
    if t in {"catch", "youtube", "vod", "게시판", "전체 게시판", "유튜브"}:
        return True
    if "catch" in u or "youtube" in u:
        return True
    if th.endswith(".svg") or "ico_lnb" in th:
        return True
    if "station/" in u and "/vod/review" in u and u.rstrip("/").endswith("/vod/review"):
        return True
    return False


async def extract_latest_vod(page) -> Dict[str, Any]:
    await goto(page, VOD_URL, "vod")
    await debug_save(page, "vod_page")

    # 카드형 다시보기 후보. 메뉴/사이드바 링크(Catch 등)는 제외한다.
    candidates = []
    anchors = await page.locator("a[href]").all()
    for a in anchors:
        try:
            href = await a.get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://www.sooplive.com" + href
            if "vod" not in href.lower():
                continue

            text = clean_text(await a.inner_text(timeout=800))
            img = a.locator("img").first
            thumb = ""
            if await img.count():
                thumb = await img.get_attribute("src") or await img.get_attribute("data-src") or await img.get_attribute("data-original") or ""
                if thumb.startswith("//"):
                    thumb = "https:" + thumb
                elif thumb.startswith("/"):
                    thumb = "https://www.sooplive.com" + thumb

            if is_bad_vod(text, href, thumb):
                continue

            # 긴 카드 텍스트를 제목 후보로 사용
            try:
                card_text = clean_text(await a.locator("xpath=ancestor::*[self::li or self::div][1]").inner_text(timeout=800))
                if len(card_text) > len(text):
                    text = card_text
            except Exception:
                pass

            candidates.append({
                "title": text[:160] or "최근 다시보기",
                "url": href,
                "thumb": thumb,
                "source": VOD_URL,
                "scrapedAt": now_kst_iso(),
            })
        except Exception:
            continue

    if candidates:
        print(f"[vod] candidates={len(candidates)}")
        return candidates[0]

    return {
        "title": "다시보기를 불러오지 못했습니다",
        "url": VOD_URL,
        "thumb": "",
        "source": VOD_URL,
        "error": "NO_VALID_VOD_CARD_FOUND",
        "scrapedAt": now_kst_iso(),
    }


async def find_calendar_cells_by_geometry(page) -> List[Dict[str, Any]]:
    """
    li 기반 선택자는 게시판/VOD 메뉴를 잘못 잡는다.
    그래서 화면에 보이는 '큰 달력 7칸'을 bounding box로 찾는다.
    조건:
    - visible
    - width >= 100, height >= 90
    - 텍스트가 너무 길지 않음
    - 달력 날짜/일정 텍스트 포함
    - 같은 행(y축)에 있는 7개 큰 박스
    """
    cells = await page.evaluate("""
    () => {
      const all = Array.from(document.querySelectorAll('div,td,li,button,a'));
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const rows = [];
      for (const el of all) {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') continue;
        if (r.width < 100 || r.height < 80) continue;
        if (r.top < 40 || r.top > vh * 0.65) continue;
        if (r.left < -5 || r.right > vw + 5) continue;
        const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        if (!txt) continue;
        if (txt.length > 160) continue;
        // 날짜 숫자 또는 일정 키워드가 있어야 함
        if (!/(\\d{1,2}일|\\d{1,2}\\s|방송|휴방|합방|예정|오후|오전)/.test(txt)) continue;
        rows.push({
          tag: el.tagName,
          text: txt,
          x: r.left,
          y: r.top,
          w: r.width,
          h: r.height,
          cx: r.left + r.width / 2,
          cy: r.top + r.height / 2
        });
      }
      return rows;
    }
    """)

    # y가 비슷한 것끼리 묶고, 가장 7칸에 가까운 행 선택
    groups: List[List[Dict[str, Any]]] = []
    for c in sorted(cells, key=lambda x: (x["y"], x["x"])):
        placed = False
        for g in groups:
            if abs(g[0]["y"] - c["y"]) < 35:
                g.append(c)
                placed = True
                break
        if not placed:
            groups.append([c])

    best = []
    for g in groups:
        # 중복/부모 요소 제거: x 중심이 비슷하면 작은 영역보다 달력칸다운 큰 영역 우선
        g = sorted(g, key=lambda x: (x["x"], -x["w"] * x["h"]))
        dedup = []
        for c in g:
            if any(abs(c["cx"] - d["cx"]) < 45 for d in dedup):
                continue
            dedup.append(c)
        # 7개 이상이면 왼쪽부터 7개
        if len(dedup) >= 7:
            best = sorted(dedup, key=lambda x: x["x"])[:7]
            break
        if len(dedup) > len(best):
            best = dedup

    print("[calendar] geometry candidates:", len(best))
    for i, c in enumerate(best[:7]):
        print(f"  {i}: x={c['x']:.0f} y={c['y']:.0f} w={c['w']:.0f} h={c['h']:.0f} text={c['text'][:60]}")

    return sorted(best, key=lambda x: x["x"])[:7]


async def read_detail_after_click(page) -> str:
    # 클릭 후 아래에 생기는 상세 박스만 노린다. 달력 위쪽 전체 텍스트를 피하기 위해 y 큰 요소 위주.
    await page.wait_for_timeout(900)
    detail = await page.evaluate("""
    () => {
      const vh = window.innerHeight;
      const all = Array.from(document.querySelectorAll('div,section,article,li'));
      const cand = [];
      for (const el of all) {
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') continue;
        if (r.width < 250 || r.height < 45) continue;
        if (r.top < 210) continue; // 달력 칸 위쪽 제외
        const txt = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        if (!txt || txt.length < 2 || txt.length > 500) continue;
        // 일정 상세 박스에 자주 나오는 키워드
        if (/(오전|오후|방송|휴방|합방|예정|ASL|CK|스폰|직관|준비중)/.test(txt)) {
          cand.push({text: txt, x:r.left, y:r.top, w:r.width, h:r.height, area:r.width*r.height});
        }
      }
      cand.sort((a,b) => {
        // 너무 큰 전체 컨테이너보다 중간 크기 박스 선호
        const scoreA = Math.abs(a.w - 900) + Math.abs(a.h - 100) + a.y * 0.02;
        const scoreB = Math.abs(b.w - 900) + Math.abs(b.h - 100) + b.y * 0.02;
        return scoreA - scoreB;
      });
      return cand[0] || null;
    }
    """)
    return clean_text(detail["text"]) if detail else ""


def parse_cell_text(cell_text: str) -> Dict[str, str]:
    cell_text = clean_text(cell_text)
    date_match = re.search(r"(\d{1,2})일", cell_text)
    status = ""
    for key in ["방송 예정", "방송", "합방", "휴방", "기타"]:
        if key in cell_text:
            status = key
            break
    time_match = re.search(r"(오전|오후)\s*\d{1,2}(?::\d{2})?", cell_text)
    return {
        "date": date_match.group(1) + "일" if date_match else "",
        "status": status,
        "time": time_match.group(0) if time_match else "",
        "raw": cell_text,
    }


def parse_detail_text(text: str) -> Dict[str, str]:
    text = clean_text(text)
    if not text:
        return {"category": "", "time": "", "title": "", "raw": ""}

    category = ""
    for key in ["방송 예정", "방송", "합방", "휴방", "기타"]:
        if key in text:
            category = key
            break

    time_match = re.search(r"(오전|오후)\s*\d{1,2}(?::\d{2})?", text)
    time = time_match.group(0) if time_match else ""

    # 카테고리/시간 제거 후 남은 내용을 제목으로
    title = text
    if category:
        title = title.replace(category, " ")
    if time:
        title = title.replace(time, " ")
    title = clean_text(title)
    # 상세 박스에 카테고리/시간/제목 순으로 있으면 제목이 짧게 남는다
    if not title:
        title = text

    return {
        "category": category,
        "time": time,
        "title": title[:160],
        "raw": text,
    }


async def extract_week_schedule(page) -> Dict[str, Any]:
    await goto(page, CALENDAR_URL, "calendar")
    await debug_save(page, "calendar_initial")

    day_names = ["일", "월", "화", "수", "목", "금", "토"]
    cells = await find_calendar_cells_by_geometry(page)

    if len(cells) < 7:
        await debug_save(page, "calendar_no_7_cells")
        return {
            "items": [],
            "source": CALENDAR_URL,
            "error": "CALENDAR_7_CELLS_NOT_FOUND",
            "debugCandidates": cells,
            "scrapedAt": now_kst_iso(),
        }

    items = []
    for i, cell in enumerate(cells[:7]):
        try:
            await page.mouse.click(cell["cx"], cell["cy"])
            detail_text = await read_detail_after_click(page)
            cell_info = parse_cell_text(cell["text"])
            detail_info = parse_detail_text(detail_text)

            # 상세 박스가 없으면 셀 텍스트 기반으로라도 보관
            final = {
                "dayIndex": i,
                "dayName": day_names[i],
                "date": cell_info["date"],
                "status": detail_info["category"] or cell_info["status"],
                "time": detail_info["time"] or cell_info["time"],
                "title": detail_info["title"] or cell_info["raw"],
                "cellText": cell["text"],
                "detailText": detail_text,
            }
            items.append(final)
            print(f"[schedule] {day_names[i]} {final['date']} {final['status']} {final['time']} {final['title'][:60]}")
        except Exception as e:
            items.append({
                "dayIndex": i,
                "dayName": day_names[i],
                "date": parse_cell_text(cell["text"]).get("date", ""),
                "status": "",
                "time": "",
                "title": "",
                "cellText": cell["text"],
                "detailText": "",
                "error": str(e),
            })
            await debug_save(page, f"calendar_day_{i}_error")

    await debug_save(page, "calendar_done")
    return {
        "items": items,
        "source": CALENDAR_URL,
        "scrapedAt": now_kst_iso(),
    }


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1390, "height": 900},
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
