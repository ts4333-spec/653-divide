"""
backend.py  ·  Flask API 서버
KORMARC 653 필드 생성기 — 백엔드

엔드포인트
  POST /api/fetch_meta     : ISBN → 알라딘 도서 메타데이터 반환
  POST /api/generate_653   : 메타데이터 → CoT GPT 주제어 생성 반환

API 키 전달 방식
  - 요청 JSON body에 ttb_key / openai_key 포함 (프론트에서 전송)
  - 또는 서버 환경변수 TTB_KEY / OPENAI_KEY 로 대체 가능
    (환경변수가 있으면 body 값보다 우선)
"""

import os
import re
import unicodedata

import requests as http_requests
from flask import Flask, jsonify, request
from openai import OpenAI

app = Flask(__name__)


# ─────────────────────────────────────────────
# 헬퍼: API 키 결정 (환경변수 우선, 없으면 body)
# ─────────────────────────────────────────────
def _resolve_key(env_name: str, body_value: str) -> str:
    return os.environ.get(env_name, "").strip() or (body_value or "").strip()


# ─────────────────────────────────────────────
# 전처리 함수 (모두 백엔드에서만 처리)
# ─────────────────────────────────────────────
def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return text.lower().strip()


def _clean_author_str(s: str) -> str:
    s = re.sub(r"[\(\（\[\【].*?[\)\）\]\】]", "", s)
    s = re.sub(r"(지음|글|그림|옮김|편저|엮음|저|역|편)", "", s)
    s = re.sub(r"[,·\|/\\]", " ", s)
    return s.strip()


def _build_forbidden_set(title: str, authors: list[str]) -> set[str]:
    forbidden: set[str] = set()
    for token in _norm(title).split():
        if len(token) >= 2:
            forbidden.add(token)
            for size in range(2, min(len(token) + 1, 5)):
                for start in range(len(token) - size + 1):
                    forbidden.add(token[start : start + size])
    for author in authors:
        for token in _norm(_clean_author_str(author)).split():
            if len(token) >= 2:
                forbidden.add(token)
    return forbidden


def _should_keep_keyword(kw: str, forbidden: set[str]) -> bool:
    normed = _norm(kw)
    if not normed:
        return False
    if normed in forbidden:
        return False
    for fb in forbidden:
        if fb and fb in normed:
            return False
    return True


# ─────────────────────────────────────────────
# 알라딘 메타데이터 수집
# ─────────────────────────────────────────────
def _fetch_aladin(ttb_key: str, isbn: str) -> dict:
    url = "https://www.aladin.co.kr/ttb/api/ItemLookUp.aspx"
    params = {
        "TTBKey":     ttb_key,
        "itemIdType": "ISBN13",
        "ItemId":     isbn.strip(),
        "output":     "js",
        "Version":    "20131101",
        "OptResult":  "description,toc",
    }
    resp = http_requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data  = resp.json()
    items = data.get("item", [])
    if not items:
        raise ValueError("해당 ISBN의 도서를 알라딘에서 찾을 수 없습니다.")
    item = items[0]
    description = (
        item.get("description", "").strip()
        or item.get("fullDescription", "").strip()
    )
    return {
        "title":        item.get("title", "").strip(),
        "author":       item.get("author", "").strip(),
        "categoryName": item.get("categoryName", "").strip(),
        "description":  description,
        "toc":          item.get("toc", "").strip(),
    }


# ─────────────────────────────────────────────
# CoT GPT 생성
# ─────────────────────────────────────────────
_SYSTEM_PROMPT = """당신은 도서관 목록 전문가입니다.
아래 단계를 순서대로 따라 KORMARC 653 비통제 주제어를 생성하세요.

[1단계] 책 소개와 목차를 읽고, 이 책이 다루는 핵심 개념 3가지를 파악하세요.
[2단계] 각 핵심 개념에서 구체적인 하위 주제를 1~2개씩 뽑으세요.
[3단계] 도서관 이용자가 이 책을 찾을 때 실제로 검색할 법한 단어로 바꾸세요.
[4단계] 제목과 저자명에 이미 있는 단어는 제거하세요.
[5단계] 최종 5~7개를 $a기호로 구분해서 정리하세요.

[출력 규칙]
- 1~5단계의 사고 과정을 모두 서술한 뒤, 마지막에 최종 결과를 출력합니다.
- 최종 결과는 반드시 <result>와 </result> 태그 사이에만 작성하세요.
- <result> 태그 안에는 $a키워드 형식의 문자열만 넣고, 설명·번호·줄바꿈은 절대 포함하지 마세요.
- 주제어 규칙:
  · 명사형 개념어만 사용 (띄어쓰기 없는 복합명사 권장)
  · 추상적 표현 금지 ('접근', '담론', '시각', '연구', '의의', '현황', '소개', '개요')
  · 이 책만의 구체적 내용을 반영할 것
  · 중복 키워드 없이 5~7개

[출력 예시]
[1단계] 핵심 개념: ...
[2단계] 하위 주제: ...
[3단계] 검색어 변환: ...
[4단계] 제목/저자 제거: ...
[5단계] 최종 정리: ...
<result>$a아동문학$a정서조절$a그림책$a자아존중감$a공감능력</result>"""


def _generate_653(openai_key: str, meta: dict) -> dict:
    client = OpenAI(api_key=openai_key)

    user_content = (
        f"아래 도서 메타데이터를 분석하여 KORMARC 653 비통제 주제어를 생성하세요.\n\n"
        f"제목: {meta['title']}\n"
        f"저자: {meta['author']}\n"
        f"카테고리: {meta['categoryName']}\n"
        f"책 소개: {meta['description'][:800] if meta['description'] else '(없음)'}\n"
        f"목차: {meta['toc'][:600] if meta['toc'] else '(없음)'}"
    )

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        temperature=0.3,
        max_tokens=1000,
    )

    full_response = response.choices[0].message.content.strip()

    # <result> 태그 파싱
    result_match = re.search(r"<result>(.*?)</result>", full_response, re.DOTALL)
    if result_match:
        result_raw = result_match.group(1).strip()
    else:
        lines = full_response.splitlines()
        result_raw = next((ln.strip() for ln in reversed(lines) if "$a" in ln), "")

    reasoning_text = (
        full_response[: full_response.find("<result>")].strip()
        if "<result>" in full_response
        else full_response
    )

    # 금칙어 필터링
    authors   = [a.strip() for a in re.split(r"[,·|]", meta["author"]) if a.strip()]
    forbidden = _build_forbidden_set(meta["title"], authors)

    raw_keywords = [p.strip() for p in result_raw.split("$a") if p.strip()]
    filtered     = [kw for kw in raw_keywords if _should_keep_keyword(kw, forbidden)]

    seen: set[str]    = set()
    deduped: list[str] = []
    for kw in filtered:
        key = _norm(kw)
        if key not in seen:
            seen.add(key)
            deduped.append(kw)

    keywords     = deduped[:7]
    subfield_str = "".join(f"$a{kw}" for kw in keywords)
    field_653    = f"=653  \\\\{subfield_str}"

    return {
        "keywords":  keywords,
        "field_653": field_653,
        "reasoning": reasoning_text,
    }


# ─────────────────────────────────────────────
# Flask 엔드포인트
# ─────────────────────────────────────────────
@app.route("/api/fetch_meta", methods=["POST"])
def api_fetch_meta():
    """
    Request JSON:
        { "isbn": "9788932041234", "ttb_key": "ttbXXX" }
    Response JSON:
        { "title": ..., "author": ..., "categoryName": ...,
          "description": ..., "toc": ... }
    """
    body    = request.get_json(force=True, silent=True) or {}
    isbn    = (body.get("isbn") or "").strip()
    ttb_key = _resolve_key("TTB_KEY", body.get("ttb_key", ""))

    if not isbn:
        return jsonify({"error": "isbn 필드가 필요합니다."}), 400
    if not ttb_key:
        return jsonify({"error": "Aladin TTB Key가 없습니다."}), 400

    try:
        meta = _fetch_aladin(ttb_key, isbn)
        return jsonify(meta)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except http_requests.exceptions.Timeout:
        return jsonify({"error": "알라딘 API 요청 시간 초과."}), 504
    except Exception as e:
        return jsonify({"error": f"알라딘 API 오류: {e}"}), 500


@app.route("/api/generate_653", methods=["POST"])
def api_generate_653():
    """
    Request JSON:
        {
          "openai_key": "sk-...",   (환경변수 OPENAI_KEY 로 대체 가능)
          "title": ..., "author": ..., "categoryName": ...,
          "description": ..., "toc": ...
        }
    Response JSON:
        { "keywords": [...], "field_653": "=653  \\...", "reasoning": "..." }
    """
    body       = request.get_json(force=True, silent=True) or {}
    openai_key = _resolve_key("OPENAI_KEY", body.get("openai_key", ""))

    if not openai_key:
        return jsonify({"error": "OpenAI API Key가 없습니다."}), 400

    meta = {
        "title":        (body.get("title")        or "").strip(),
        "author":       (body.get("author")       or "").strip(),
        "categoryName": (body.get("categoryName") or "").strip(),
        "description":  (body.get("description")  or "").strip(),
        "toc":          (body.get("toc")          or "").strip(),
    }
    if not meta["title"]:
        return jsonify({"error": "title 필드가 필요합니다."}), 400

    try:
        result = _generate_653(openai_key, meta)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"GPT 호출 오류: {e}"}), 500


# ─────────────────────────────────────────────
# 헬스체크 (Render 업타임 확인용)
# ─────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
