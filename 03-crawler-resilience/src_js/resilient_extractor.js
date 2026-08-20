/*
 * resilient_extractor.js
 *
 * 커뮤니티 게시글 크롤러의 "개편 내성(resilience)" 추출 로직을 개념만
 * 재구성한 예시입니다. 실제 업무에서 사용한 사내 크롤러 엔진 스크립트가
 * 아니라, 그때 적용한 설계 방식을 설명하기 위해 jsoup 스타일 DOM API를
 * 가정하고 새로 작성한 데모입니다. (doc.select(...) 는 jsoup 유사 인터페이스)
 *
 * 핵심 아이디어
 *  - 값마다 신뢰도 순으로 여러 추출 전략을 두고 실패 시 자동 폴백한다.
 *      1) JSON-LD (application/ld+json)  : 마크업 변경에 가장 강함
 *      2) OpenGraph / meta 태그          : 사이트 공통 보유
 *      3) 안정적 시맨틱 셀렉터            : data-* 속성, id 접미사 등
 *      4) 정규식 기반 텍스트 추출         : 최후의 수단
 *  - 빌드마다 바뀌는 해시 클래스(예: svelte-a1b2c3)는 셀렉터에 절대 쓰지 않는다.
 *  - 날짜(오전/오후·AM/PM·ISO8601)와 통계(콤마·비숫자)를 일관 포맷으로 정규화한다.
 */

// ---------- 값 정규화 유틸 ----------
function toInt(text) {
  if (!text) return 0;
  var m = String(text).match(/[\d,]+/);
  if (!m) return 0;
  var n = parseInt(m[0].replace(/,/g, ""), 10);
  return isNaN(n) ? 0 : n;
}

// 다양한 날짜 표기를 'YYYY-MM-DD HH:MM:SS' 로 통일. 실패 시 null.
function normalizeDatetime(text) {
  if (!text) return null;

  // ISO 8601 (타임존 포함): 2026-03-09T15:17:00+09:00
  var iso = String(text).match(/(20\d{2})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (iso) {
    return iso[1] + "-" + iso[2] + "-" + iso[3] + " " +
           iso[4] + ":" + iso[5] + ":" + (iso[6] || "00");
  }

  // 한국어 오전/오후 및 영문 AM/PM 혼용: "2026년 3월 9일 오후 3:17"
  var ko = String(text).match(/(20\d{2})\D+?(\d{1,2})\D+?(\d{1,2})\D+?(오전|오후|AM|PM)\s*(\d{1,2}):(\d{2})/);
  if (ko) {
    var hour = parseInt(ko[5], 10);
    var isPM = (ko[4] === "오후" || ko[4] === "PM");
    if (isPM && hour < 12) hour += 12;
    else if (!isPM && hour === 12) hour = 0;
    var pad = function (v) { return ("0" + v).slice(-2); };
    return ko[1] + "-" + pad(ko[2]) + "-" + pad(ko[3]) + " " +
           pad(hour) + ":" + ko[6] + ":00";
  }
  return null;
}

// ---------- JSON-LD 파서 (1순위) ----------
var ARTICLE_TYPES = { Article: 1, BlogPosting: 1, DiscussionForumPosting: 1, NewsArticle: 1 };

function parseJsonLd(doc) {
  var out = {};
  var nodes = doc.select('script[type="application/ld+json"]');
  for (var i = 0; i < nodes.size(); i++) {
    var raw = nodes.get(i).data() || nodes.get(i).html();
    if (!raw) continue;

    var data;
    try { data = JSON.parse(raw); } catch (e) { continue; } // 깨진 JSON 은 건너뜀

    var arr = Object.prototype.toString.call(data) === "[object Array]" ? data : [data];
    for (var j = 0; j < arr.length; j++) {
      var node = arr[j];
      if (!node || !node["@type"] || !ARTICLE_TYPES[node["@type"]]) continue;

      if (node.headline && !out.title) out.title = String(node.headline).trim();
      var body = node.articleBody || node.text;
      if (body && !out.content) out.content = String(body).trim();
      var dt = normalizeDatetime(node.datePublished);
      if (dt && !out.postTime) out.postTime = dt;

      // 댓글수: comment[] 배열은 truncated 될 수 있어 집계 필드만 신뢰
      if (node.interactionStatistic) {
        var stats = Object.prototype.toString.call(node.interactionStatistic) === "[object Array]"
          ? node.interactionStatistic : [node.interactionStatistic];
        for (var k = 0; k < stats.length; k++) {
          var it = stats[k];
          if (it && it.interactionType &&
              String(it.interactionType).indexOf("CommentAction") !== -1 &&
              it.userInteractionCount != null) {
            out.commentCount = toInt(String(it.userInteractionCount));
          }
        }
      }
    }
  }
  return out;
}

// ---------- 폴백 체인 실행기 ----------
// 전략 함수들을 순서대로 실행, 처음으로 비어있지 않은 값을 반환.
function firstNonEmpty(strategies) {
  for (var i = 0; i < strategies.length; i++) {
    var val = null;
    try { val = strategies[i](); } catch (e) { val = null; }
    if (val && String(val).trim() !== "") {
      return { value: String(val).trim(), strategy: i };
    }
  }
  return { value: "", strategy: -1 };
}

// 안전한 셀렉터 헬퍼 (없으면 null 반환)
function txt(doc, selector) {
  var el = doc.select(selector).first();
  return el != null ? el.text() : null;
}
function attr(doc, selector, name) {
  var el = doc.select(selector).first();
  return el != null ? el.attr(name) : null;
}

// ---------- 메인 추출기 ----------
function extractPost(doc) {
  var ld = parseJsonLd(doc);

  var title = firstNonEmpty([
    function () { return ld.title; },
    function () { return attr(doc, 'meta[property="og:title"]', "content"); },
    function () { return txt(doc, "[data-slot=card-title]"); },
    function () { return txt(doc, "h1"); }
  ]);

  // 본문: id$=post-content 는 게시판별 접두어(economy-/free- 등)에 대응하는 접미사 매칭.
  // data-slot=card-content 는 공용 슬롯이라 댓글 카드와 겹칠 수 있어 우선순위를 낮춤.
  var content = firstNonEmpty([
    function () { return ld.content; },
    function () { return txt(doc, 'div[id$=post-content]'); },
    function () { return txt(doc, "[data-slot=card-content] .prose"); },
    function () { return txt(doc, "[data-slot=card-content]"); },
    function () { return attr(doc, 'meta[name=description]', "content"); }
  ]);

  var postTime = ld.postTime || normalizeDatetime(txt(doc, ".post-meta")) || "0000-00-00 00:00:00";

  var statsText = doc.select(".post-meta").text() || "";
  var viewCount = 0, commentCount = 0;

  var vm = statsText.match(/조회\s*([\d,]+)/);
  viewCount = vm ? toInt(vm[1]) : toInt((doc.html().match(/\bviews?:(\d+)/) || [])[1]);

  if (ld.commentCount != null) {
    commentCount = ld.commentCount;
  } else {
    var cm = statsText.match(/댓글\s*([\d,]+)/);
    commentCount = cm ? toInt(cm[1]) : toInt((doc.html().match(/\bcomments?_count:(\d+)/) || [])[1]);
  }

  return {
    title: title.value,
    content: content.value,
    postTime: postTime,
    viewCount: viewCount,
    commentCount: commentCount,
    // 각 필드가 몇 순위 전략에서 나왔는지 (모니터링/디버깅용)
    sources: { title: title.strategy, content: content.strategy }
  };
}

// 모듈/브라우저 양쪽에서 쓸 수 있게 노출
if (typeof module !== "undefined" && module.exports) {
  module.exports = { extractPost: extractPost, normalizeDatetime: normalizeDatetime, toInt: toInt };
}
