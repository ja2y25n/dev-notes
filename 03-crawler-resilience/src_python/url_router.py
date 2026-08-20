"""
url_router.py

크롤러의 "수집 대상 URL 판별" 로직을 개념만 재구성한 예시입니다.
실제 사내 크롤러 설정이 아니라, 게시판 추가/사이트 개편에 대응하며
정규식 패턴을 확장했던 방식을 범용 Python 으로 새로 작성한 데모입니다.

배경
----
새 게시판을 수집 대상에 추가하거나 사이트가 URL 체계를 바꿀 때마다
목록(list) URL 과 게시글(article) URL 을 구분하는 정규식을 수정해야 했다.
자주 나온 케이스:
  - 한 사이트에서 게시판 하나를 더 편입 → 기존 패턴에 OR 분기 추가
      예) .../pt?page=N   →   .../(pt|name)?page=N
      예) id=fun&page=N   →   id=(fun|news2)&page=N
  - 사이트 개편으로 URL 형태 자체가 변경 (쿼리스트링 → path 기반 등)
      예) board.php?bo_table=xxx&page=N   →   /b/xxx/list?page=N
  - 게시글 링크에 꼬리 파라미터가 붙어 중복 수집되는 문제
      → 정규식을 느슨한 매칭에서 명시적 매칭으로 좁혀 중복 제거

설계 포인트
-----------
패턴을 코드 곳곳에 흩어두지 않고 사이트별 레지스트리로 모아
"목록/게시글" 종류와 함께 관리한다. 게시판 추가 = 레지스트리에 한 줄 추가.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class UrlKind(Enum):
    LIST = "list"        # 게시글 목록 페이지
    ARTICLE = "article"  # 개별 게시글 페이지
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Rule:
    site: str
    kind: UrlKind
    pattern: re.Pattern


class UrlRouter:
    """사이트별 목록/게시글 URL 판별기. 게시판 추가 시 규칙만 등록하면 됨."""

    def __init__(self) -> None:
        self._rules: list[Rule] = []

    def register(self, site: str, kind: UrlKind, regex: str) -> None:
        self._rules.append(Rule(site, kind, re.compile(regex)))

    def classify(self, url: str) -> tuple[str, UrlKind]:
        for rule in self._rules:
            if rule.pattern.search(url):
                return rule.site, rule.kind
        return "", UrlKind.UNKNOWN

    def is_target(self, url: str) -> bool:
        _, kind = self.classify(url)
        return kind is not UrlKind.UNKNOWN


def clean_article_url(url: str, base: str = "") -> str:
    """
    게시글 URL 정규화:
      - 상대경로(..) 제거 후 base 결합
      - 목록 꼬리 파라미터(?page=, ?hit_yn= 등) 제거로 중복 수집 방지
    느슨한 매칭이 만들던 '같은 글의 다른 URL' 중복을 줄이는 단계.
    """
    url = url.replace("..", "")
    url = url.split("?")[0]          # 쿼리스트링 절단
    if base and url.startswith("/"):
        url = base.rstrip("/") + url
    return url


def build_demo_router() -> UrlRouter:
    """
    데모용 레지스트리. 사이트명은 전부 익명화(A/B/C...)했으며,
    URL 형태는 '게시판 추가 시 OR 분기를 넣는' 실제 패턴 확장 방식을 반영했다.
    """
    r = UrlRouter()

    # 커뮤니티 A: 게시판 하나를 추가하며 OR 분기 확장 (pt -> pt|name)
    r.register("community_A", UrlKind.LIST,
               r"^https://a\.example\.com/(pt|name)\?page=\d+")
    r.register("community_A", UrlKind.ARTICLE,
               r"^https://a\.example\.com/(pt|name)/\d+")

    # 커뮤니티 B: 유머 게시판에 뉴스 게시판 편입 (fun -> fun|news2)
    r.register("community_B", UrlKind.LIST,
               r"^https://b\.example\.com/pb\.php\?id=(fun|news2)&page=\d+")
    r.register("community_B", UrlKind.ARTICLE,
               r"^https://b\.example\.com/(fun|news2)/\d+")

    # 커뮤니티 C: 개편으로 쿼리스트링 -> path 기반으로 URL 체계 변경
    #   (구) board.php?bo_table=xxx&page=N   (신) /b/xxx/list?page=N
    r.register("community_C", UrlKind.LIST,
               r"^https://c\.example\.com/b/[a-z0-9]+/list\?page=\d+")
    r.register("community_C", UrlKind.ARTICLE,
               r"^https://c\.example\.com/b/[a-z0-9]+/view/[^?]+")

    return r


if __name__ == "__main__":
    router = build_demo_router()
    samples = [
        "https://a.example.com/pt?page=1",                 # A 목록(기존)
        "https://a.example.com/name?page=3",               # A 목록(추가된 게시판)
        "https://a.example.com/name/1024",                 # A 게시글
        "https://b.example.com/pb.php?id=news2&page=2",     # B 목록(추가된 뉴스판)
        "https://c.example.com/b/sisa/list?page=1",         # C 목록(개편 후)
        "https://c.example.com/b/sisa/view/hello-9007615?hit_yn=y&page=1",  # C 게시글(+꼬리)
        "https://unknown.example.com/whatever",             # 미등록
    ]
    for u in samples:
        site, kind = router.classify(u)
        line = f"[{kind.value:8}] {site or '-':12} {u}"
        if kind is UrlKind.ARTICLE:
            line += f"\n            -> cleaned: {clean_article_url(u)}"
        print(line)
