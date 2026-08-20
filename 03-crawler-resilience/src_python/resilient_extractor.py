"""
resilient_extractor.py

커뮤니티 게시글 크롤러의 "개편 내성(resilience)" 추출 로직을 개념만 재구성한 예시입니다.
실제 업무에서 사용한 사내 크롤러 엔진 코드가 아니라, 그때 적용한 설계 방식을
설명하기 위해 범용 Python(requests + BeautifulSoup)으로 새로 작성한 데모입니다.

배경
----
수십 개 커뮤니티 사이트를 수집하는 크롤러를 유지보수하면서 가장 잦았던 장애는
(1) 사이트 개편으로 HTML 구조·클래스명이 바뀌어 셀렉터가 깨지는 것,
(2) 특정 SPA(예: SvelteKit 등)가 배포마다 해시 클래스(예: svelte-a1b2c3)를 새로 생성해
    클래스 기반 셀렉터를 신뢰할 수 없게 되는 것,
(3) 날짜/조회수 표기가 사이트마다 제각각(오전·오후, AM·PM, ISO8601, 콤마 등)인 것이었다.

핵심 아이디어
-------------
"하나의 셀렉터에 의존하지 않는다." 값마다 신뢰도 순으로 여러 추출 전략을 세우고
앞 전략이 실패하면 다음으로 자동 폴백한다. 우선순위는 마크업 변경에 강한 것부터:

    1) JSON-LD (application/ld+json) : 구조화 데이터. 화면 클래스가 바뀌어도 유지됨
    2) OpenGraph / 표준 meta 태그    : 대부분의 사이트가 공통 보유
    3) 안정적 시맨틱 셀렉터           : data-* 속성, id 접미사 등 빌드 비의존 요소
    4) 정규식 기반 텍스트 추출        : 최후의 수단

빌드마다 바뀌는 해시 클래스는 셀렉터에 절대 쓰지 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# 결과 모델
# ---------------------------------------------------------------------------
@dataclass
class Post:
    title: str = ""
    content: str = ""
    post_time: str = "0000-00-00 00:00:00"  # YYYY-MM-DD HH:MM:SS 로 정규화
    view_count: int = 0
    comment_count: int = 0
    # 어떤 전략으로 각 필드를 얻었는지 기록 (디버깅/모니터링용)
    sources: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 값 정규화 유틸
# ---------------------------------------------------------------------------
_INT_RE = re.compile(r"[\d,]+")
_ISO_RE = re.compile(
    r"(20\d{2})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?"
)
_KO_DATE_RE = re.compile(
    r"(20\d{2})\D+?(\d{1,2})\D+?(\d{1,2})\D+?(오전|오후|AM|PM)\s*(\d{1,2}):(\d{2})"
)


def to_int(text: Optional[str]) -> int:
    """'조회 1,649' / '조회수 42' / None -> 1649 / 42 / 0"""
    if not text:
        return 0
    m = _INT_RE.search(text)
    if not m:
        return 0
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return 0


def normalize_datetime(text: Optional[str]) -> Optional[str]:
    """
    다양한 날짜 표기를 'YYYY-MM-DD HH:MM:SS' 로 통일.
    - ISO 8601 (타임존 포함) : 2026-03-09T15:17:00+09:00
    - 한국어 오전/오후, 영문 AM/PM 혼용
    실패 시 None 반환 -> 호출부에서 다음 폴백으로 진행.
    """
    if not text:
        return None

    iso = _ISO_RE.search(text)
    if iso:
        y, mo, d, h, mi, s = iso.groups()
        return f"{y}-{mo}-{d} {h}:{mi}:{s or '00'}"

    ko = _KO_DATE_RE.search(text)
    if ko:
        y, mo, d, ampm, h, mi = ko.groups()
        hour = int(h)
        is_pm = ampm in ("오후", "PM")
        if is_pm and hour < 12:
            hour += 12
        elif not is_pm and hour == 12:
            hour = 0
        return f"{y}-{int(mo):02d}-{int(d):02d} {hour:02d}:{mi}:00"

    return None


# ---------------------------------------------------------------------------
# JSON-LD 파서 (1순위 전략)
# ---------------------------------------------------------------------------
_ARTICLE_TYPES = {"Article", "BlogPosting", "DiscussionForumPosting", "NewsArticle"}


def parse_json_ld(soup: BeautifulSoup) -> dict:
    """
    <script type="application/ld+json"> 안의 구조화 데이터에서
    제목/본문/작성일/댓글수를 추출. 배열/단일 객체 모두 대응.
    구조화 데이터는 화면 마크업이 바뀌어도 잘 유지되므로 가장 신뢰도가 높다.
    """
    out: dict = {}
    for tag in soup.select('script[type="application/ld+json"]'):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue  # 깨진 JSON 은 조용히 건너뛰고 다음 후보로

        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            if node.get("@type") not in _ARTICLE_TYPES:
                continue

            if "headline" in node and "title" not in out:
                out["title"] = str(node["headline"]).strip()
            body = node.get("articleBody") or node.get("text")
            if body and "content" not in out:
                out["content"] = str(body).strip()
            dt = normalize_datetime(node.get("datePublished"))
            if dt and "post_time" not in out:
                out["post_time"] = dt

            # 댓글수: comment[] 배열은 앞 몇 개만 잘려 담기는(truncated) 경우가 있어
            # 신뢰하지 않고, 집계 필드(interactionStatistic)만 사용한다.
            stats = node.get("interactionStatistic")
            if stats:
                for it in (stats if isinstance(stats, list) else [stats]):
                    if not isinstance(it, dict):
                        continue
                    itype = str(it.get("interactionType", ""))
                    if "CommentAction" in itype and it.get("userInteractionCount") is not None:
                        out["comment_count"] = to_int(str(it["userInteractionCount"]))
    return out


# ---------------------------------------------------------------------------
# 폴백 체인 실행기
# ---------------------------------------------------------------------------
def first_nonempty(*strategies: Callable[[], Optional[str]]) -> tuple[str, int]:
    """
    전략들을 순서대로 실행하여 처음으로 비어있지 않은 값을 반환.
    반환: (값, 사용된 전략 인덱스). 모두 실패하면 ('', -1).
    """
    for idx, strat in enumerate(strategies):
        try:
            val = strat()
        except Exception:
            val = None
        if val and str(val).strip():
            return str(val).strip(), idx
    return "", -1


# ---------------------------------------------------------------------------
# 메인 추출기
# ---------------------------------------------------------------------------
def extract_post(html: str) -> Post:
    soup = BeautifulSoup(html, "html.parser")
    ld = parse_json_ld(soup)
    post = Post()

    # ---- 제목: JSON-LD -> og:title -> 안정 셀렉터 -> h1 ----
    title, ti = first_nonempty(
        lambda: ld.get("title"),
        lambda: soup.select_one('meta[property="og:title"]')["content"]
        if soup.select_one('meta[property="og:title"]') else None,
        lambda: soup.select_one("[data-slot=card-title]").get_text()
        if soup.select_one("[data-slot=card-title]") else None,
        lambda: soup.select_one("h1").get_text() if soup.select_one("h1") else None,
    )
    post.title = title
    post.sources["title"] = ti

    # ---- 본문: JSON-LD -> id 접미사(post-content) -> data-slot 본문 -> meta description ----
    #   * id$=post-content : 게시판별 접두어(economy-/free- 등)가 붙어도 대응하는 접미사 매칭
    #   * data-slot=card-content 는 공용 슬롯이라 댓글 카드와 겹칠 수 있어 우선순위를 낮춤
    content, ci = first_nonempty(
        lambda: ld.get("content"),
        lambda: soup.select_one('[id$="post-content"]').get_text()
        if soup.select_one('[id$="post-content"]') else None,
        lambda: soup.select_one("[data-slot=card-content] .prose").get_text()
        if soup.select_one("[data-slot=card-content] .prose") else None,
        lambda: soup.select_one("[data-slot=card-content]").get_text()
        if soup.select_one("[data-slot=card-content]") else None,
        lambda: soup.select_one('meta[name="description"]')["content"]
        if soup.select_one('meta[name="description"]') else None,
    )
    post.content = content
    post.sources["content"] = ci

    # ---- 작성일: JSON-LD -> 화면 텍스트 정규화 ----
    if "post_time" in ld:
        post.post_time = ld["post_time"]
        post.sources["post_time"] = 0
    else:
        stats_node = soup.select_one(".post-meta")  # 데모용 안정 셀렉터
        dt = normalize_datetime(stats_node.get_text() if stats_node else None)
        if dt:
            post.post_time = dt
            post.sources["post_time"] = 1

    # ---- 통계: 화면 텍스트("조회 N"/"댓글 N") -> 페이지 임베드 데이터 폴백 ----
    stats_text = " ".join(n.get_text() for n in soup.select(".post-meta")) or ""
    vm = re.search(r"조회\s*([\d,]+)", stats_text)
    if vm:
        post.view_count = to_int(vm.group(1))
        post.sources["view_count"] = 0
    else:
        m = re.search(r"\bviews?:(\d+)", html)  # SPA 임베드 데이터 폴백
        post.view_count = int(m.group(1)) if m else 0
        post.sources["view_count"] = 1 if m else -1

    if "comment_count" in ld:
        post.comment_count = ld["comment_count"]
        post.sources["comment_count"] = 0
    else:
        cm = re.search(r"댓글\s*([\d,]+)", stats_text)
        if cm:
            post.comment_count = to_int(cm.group(1))
            post.sources["comment_count"] = 1
        else:
            m = re.search(r"\bcomments?_count:(\d+)", html)
            post.comment_count = int(m.group(1)) if m else 0
            post.sources["comment_count"] = 2 if m else -1

    return post


if __name__ == "__main__":
    # 최소 동작 예시 (실제 사이트가 아닌 합성 HTML)
    demo_html = """
    <html><head>
      <script type="application/ld+json">
      {"@type":"DiscussionForumPosting","headline":"예시 제목",
       "articleBody":"예시 본문입니다.","datePublished":"2026-03-09T15:17:00+09:00",
       "interactionStatistic":{"interactionType":"https://schema.org/CommentAction",
       "userInteractionCount":5}}
      </script>
    </head><body>
      <div class="post-meta">2026년 3월 9일 오후 3:17 · 조회 1,649 · 댓글 5</div>
    </body></html>
    """
    p = extract_post(demo_html)
    print("title      :", p.title)
    print("content    :", p.content)
    print("post_time  :", p.post_time)
    print("view_count :", p.view_count)
    print("comments   :", p.comment_count)
    print("sources    :", p.sources)  # 각 필드가 몇 순위 전략에서 나왔는지
