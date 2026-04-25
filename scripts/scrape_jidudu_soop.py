import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

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
    t = (title or "").strip().lower()
    u = (url or "").lower()
    th = (thumb or "").lower()

    bad_exact = {"catch", "youtube", "vod", "게시판", "전체 게시판", "유튜브"}
    if not t or t in bad_exact:
        return True
    if "catch" in u or "youtube" in u:
        return True
    if th.endswith(".svg") or "ico_lnb" in th:
        return True
    if u.rstrip("/").endswith("/vod/review"):
        return True
    return False


async def extract_latest_vod(page) -> Dict[str, Any]:
    await goto(page, VOD_URL, "vod")
    await debug_save(page, "vod_page")

    # VOD 카드 후보를 넓게 잡되, 메뉴(Catch/YouTube/게시판)는 제외
    cards = await page.locator("a[href]").all()
    candidates = []

    for a in cards:
        try:
            href = await a.get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://www.sooplive.com" + href

            href_l = href.lower()

            # 실제 다시보기/영상 링크 후보만
            if not (
                "vod.sooplive.com/player" in href_l
                or "/vod/" in href_l
                or "/review/" in href_l
            ):
                continue

            # 메뉴성 링크 제외
            if any(bad in href_l for bad in ["catch", "youtube", "board"]):
                continue
            if href_l.rstrip("/").endswith("/vod/review"):
                continue

            # 카드 전체 텍스트
            title = clean_text(await a.inner_text(timeout=1000))

            try:
                card_text = clean_text(
                    await a.locator("xpath=ancestor::*[self::li or self::div][1]").inner_text(timeout=1000)
                )
                if len(card_text) > len(title):
                    title = card_text
            except Exception:
                pass

            if not title or title.lower() in {"catch", "youtube", "vod"}:
                continue

            # 썸네일 찾기: 링크 내부 img → 부모 카드 img 순서
            thumb = ""

            img = a.locator("img").first
            if await img.count():
                thumb = (
                    await img.get_attribute("src")
                    or await img.get_attribute("data-src")
                    or await img.get_attribute("data-original")
                    or await img.get_attribute("data-lazy")
                    or ""
                )

            if not thumb:
                try:
                    parent_img = a.locator("xpath=ancestor::*[self::li or self::div][1]//img").first
                    if await parent_img.count():
                        thumb = (
                            await parent_img.get_attribute("src")
                            or await parent_img.get_attribute("data-src")
                            or await parent_img.get_attribute("data-original")
                            or await parent_img.get_attribute("data-lazy")
                            or ""
                        )
                except Exception:
                    pass

            # background-image 썸네일 fallback
            if not thumb:
                try:
                    bg = await a.evaluate("""
                    el => {
                      const nodes = [el, ...el.querySelectorAll('*')];
                      for (const n of nodes) {
                        const bg = getComputedStyle(n).backgroundImage || '';
                        if (bg.includes('url(')) return bg;
                      }
                      const p = el.closest('li,div');
                      if (p) {
                        const nodes2 = [p, ...p.querySelectorAll('*')];
                        for (const n of nodes2) {
                          const bg = getComputedStyle(n).backgroundImage || '';
                          if (bg.includes('url(')) return bg;
                        }
                      }
                      return '';
                    }
                    """)
                    m = re.search(r'url\\(["\\']?(.*?)["\\']?\\)', bg or "")
                    if m:
                        thumb = m.group(1)
                except Exception:
                    pass

            if thumb.startswith("//"):
                thumb = "https:" + thumb
            elif thumb.startswith("/"):
                thumb = "https://www.sooplive.com" + thumb

            # 기본 아이콘/svg 제외
            thumb_l = thumb.lower()
            if thumb_l.endswith(".svg") or "ico_lnb" in thumb_l or "catch" in thumb_l:
                thumb = ""

            candidates.append({
                "title": title[:160],
                "url": href,
                "thumb": thumb,
                "source": VOD_URL,
                "scrapedAt": now_kst_iso(),
            })

        except Exception:
            continue

    if candidates:
        # 썸네일 있는 후보를 우선 선택
        candidates.sort(key=lambda x: 0 if x.get("thumb") else 1)
        print(f"[vod] candidates={len(candidates)}, selected_thumb={bool(candidates[0].get('thumb'))}")
        return candidates[0]

    return {
        "title": "다시보기를 불러오지 못했습니다",
        "url": VOD_URL,
        "thumb": "",
        "source": VOD_URL,
        "error": "NO_VALID_VOD_CARD_FOUND",
        "scrapedAt": now_kst_iso(),
    }


def parse_cell_text(text: str) -> Dict[str, str]:
    text = clean_text(text)
    date_match = re.search(r"(\d{1,2})일", text)
    time_match = re.search(r"(오전|오후)\s*\d{1,2}(?::\d{2})?", text)

    status = ""
    for key in ["방송 예정", "방송", "합방", "휴방", "기타"]:
        if key in text:
            status = key
            break

    return {
        "date": f"{date_match.group(1)}일" if date_match else "",
        "status": status,
        "time": time_match.group(0) if time_match else "",
        "raw": text,
    }


def parse_detail_text(text: str) -> Dict[str, str]:
    text = clean_text(text)
    if not text:
        return {"status": "", "time": "", "title": "", "raw": ""}

    # 주간 전체 텍스트가 들어오면 무효 처리
    date_count = len(re.findall(r"\d{1,2}일", text))
    if date_count >= 3:
        return {"status": "", "time": "", "title": "", "raw": text, "invalid": "WEEK_SUMMARY_TEXT"}

    status = ""
    for key in ["방송 예정", "방송", "합방", "휴방", "기타"]:
        if key in text:
            status = key
            break

    time_match = re.search(r"(오전|오후)\s*\d{1,2}(?::\d{2})?", text)
    time = time_match.group(0) if time_match else ""

    title = text
    if status:
        title = title.replace(status, " ")
    if time:
        title = title.replace(time, " ")
    title = clean_text(title)

    # 휴방은 제목도 휴방 처리
    if status == "휴방" and not title:
        title = "휴방"

    return {
        "status": status,
        "time": time,
        "title": title[:160] if title else status,
        "raw": text,
    }


async def get_week_calendar_grid(page) -> Dict[str, Any]:
    """
    v3 핵심:
    1) 실제 화면에서 '일 월 화 수 목 금 토' 헤더 7개를 찾음
    2) 헤더 바로 아래의 같은 x축 7개 날짜칸 중앙 좌표를 계산
    3) li/메뉴/게시판 후보는 완전히 배제
    """
    grid = await page.evaluate("""
    () => {
      const dayNames = ['일','월','화','수','목','금','토'];
      const visible = el => {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 20 && r.height > 10;
      };

      const els = Array.from(document.querySelectorAll('div,span,th,td,li,button'));
      const dayEls = [];
      for (const el of els) {
        if (!visible(el)) continue;
        const txt = (el.innerText || '').replace(/\\s+/g,' ').trim();
        if (dayNames.includes(txt)) {
          const r = el.getBoundingClientRect();
          dayEls.push({txt, x:r.left, y:r.top, w:r.width, h:r.height, cx:r.left+r.width/2, cy:r.top+r.height/2});
        }
      }

      // y가 비슷하고 일월화수목금토 순서가 맞는 헤더 행 찾기
      const groups = [];
      for (const d of dayEls.sort((a,b)=>a.y-b.y || a.x-b.x)) {
        let g = groups.find(row => Math.abs(row[0].y - d.y) < 20);
        if (!g) { g = []; groups.push(g); }
        g.push(d);
      }

      let header = null;
      for (const g of groups) {
        const sorted = g.sort((a,b)=>a.x-b.x);
        const seq = sorted.map(x=>x.txt).join('');
        if (sorted.length >= 7 && seq.includes('일월화수목금토')) {
          header = sorted.slice(0,7);
          break;
        }
      }

      if (!header) return {error:'NO_WEEK_HEADER', dayEls};

      const xs = header.map(h => h.cx);
      const headerY = Math.max(...header.map(h => h.y + h.h));

      // 헤더 아래 큰 날짜칸 후보
      const blocks = [];
      const all = Array.from(document.querySelectorAll('div,td,li,button'));
      for (const el of all) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        const txt = (el.innerText || '').replace(/\\s+/g,' ').trim();
        if (r.top < headerY - 5) continue;
        if (r.top > headerY + 260) continue;
        if (r.width < 80 || r.height < 60) continue;
        if (!/\\d{1,2}일/.test(txt)) continue;
        if (txt.length > 70) continue;
        if (/열혈팬|구독|랭킹|게시판|VOD|하단메뉴|전체메뉴/.test(txt)) continue;
        blocks.push({
          text: txt,
          x:r.left, y:r.top, w:r.width, h:r.height,
          cx:r.left+r.width/2, cy:r.top+r.height/2
        });
      }

      // 각 요일 헤더 x축과 가장 가까운 날짜칸 1개씩 매칭
      const cells = xs.map((x, idx) => {
        const near = blocks
          .filter(b => Math.abs(b.cx - x) < Math.max(90, b.w/1.2))
          .sort((a,b) => Math.abs(a.cx-x)-Math.abs(b.cx-x) || a.y-b.y)[0];
        if (near) return {...near, method:'block-match', dayIndex:idx};

        // 후보가 없으면 헤더 x + 날짜칸 예상 y로 좌표 fallback
        return {
          text:'',
          x:x-40,
          y:headerY+55,
          w:80,
          h:90,
          cx:x,
          cy:headerY+95,
          method:'coordinate-fallback',
          dayIndex:idx
        };
      });

      return {header, blocks, cells};
    }
    """)
    return grid


async def read_detail_after_click(page, clicked_cy: float) -> str:
    await page.wait_for_timeout(850)

    # 클릭한 날짜칸 아래쪽에 생긴 상세 박스만 잡는다.
    detail = await page.evaluate("""
    (clickedCy) => {
      const all = Array.from(document.querySelectorAll('div,section,article,li'));
      const cand = [];
      for (const el of all) {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        if (s.display === 'none' || s.visibility === 'hidden') continue;
        if (r.width < 240 || r.height < 45) continue;
        if (r.top < clickedCy + 15) continue;
        const txt = (el.innerText || '').replace(/\\s+/g,' ').trim();
        if (!txt || txt.length < 2 || txt.length > 260) continue;
        if (/열혈팬|구독|랭킹|게시판|VOD|하단메뉴|전체메뉴|공유하기/.test(txt)) continue;

        const dateCount = (txt.match(/\\d{1,2}일/g) || []).length;
        if (dateCount >= 3) continue; // 주간 전체 요약 제외

        if (/(오전|오후|방송|휴방|합방|예정|ASL|CK|스폰|직관|준비중)/.test(txt)) {
          cand.push({
            text: txt,
            x:r.left, y:r.top, w:r.width, h:r.height,
            score: Math.abs(r.width - 900) + Math.abs(r.height - 100) + Math.abs(r.top - (clickedCy+35))
          });
        }
      }
      cand.sort((a,b)=>a.score-b.score);
      return cand[0] || null;
    }
    """, clicked_cy)

    return clean_text(detail["text"]) if detail else ""


async def extract_week_schedule(page) -> Dict[str, Any]:
    await goto(page, CALENDAR_URL, "calendar")
    await debug_save(page, "calendar_initial")

    day_names = ["일", "월", "화", "수", "목", "금", "토"]
    grid = await get_week_calendar_grid(page)

    if grid.get("error"):
        await debug_save(page, "calendar_grid_error")
        return {
            "items": [],
            "source": CALENDAR_URL,
            "error": grid.get("error"),
            "debug": grid,
            "scrapedAt": now_kst_iso(),
        }

    cells = grid.get("cells", [])
    print("[calendar] cells:")
    for c in cells:
        print(f"  {c.get('dayIndex')}: {c.get('method')} {c.get('text')} x={c.get('cx')} y={c.get('cy')}")

    items = []
    for i, cell in enumerate(cells[:7]):
        try:
            await page.mouse.click(float(cell["cx"]), float(cell["cy"]))
            detail_text = await read_detail_after_click(page, float(cell["cy"]))

            cell_info = parse_cell_text(cell.get("text", ""))
            detail_info = parse_detail_text(detail_text)

            status = detail_info.get("status") or cell_info.get("status") or ""
            time = detail_info.get("time") or cell_info.get("time") or ""
            title = detail_info.get("title") or ("일정 없음" if not status else status)

            # 상세가 주간 전체라 무효 처리됐으면 셀 정보만 사용
            if detail_info.get("invalid"):
                title = status or "일정 없음"
                detail_text = ""

            item = {
                "dayIndex": i,
                "dayName": day_names[i],
                "date": cell_info.get("date", ""),
                "status": status,
                "time": time,
                "title": title,
                "cellText": cell.get("text", ""),
                "detailText": detail_text,
                "clickMethod": cell.get("method", ""),
            }
            items.append(item)
            print(f"[schedule] {item}")
        except Exception as e:
            items.append({
                "dayIndex": i,
                "dayName": day_names[i],
                "date": parse_cell_text(cell.get("text", "")).get("date", ""),
                "status": "",
                "time": "",
                "title": "수집 실패",
                "cellText": cell.get("text", ""),
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
