"""
frontend.py  ·  Streamlit 프론트엔드
KORMARC 653 필드 생성기

무거운 연산(알라딘 조회, GPT 생성)은 모두 Flask 백엔드에 위임하고,
UI 상태 관리(사서 검수, 편집, 확정)만 여기서 처리합니다.
"""

import re
import requests
import streamlit as st

# ─────────────────────────────────────────────
# 백엔드 주소 설정 — 배포 시 이 값만 바꾸면 됩니다
# ─────────────────────────────────────────────
BACKEND_URL = "http://localhost:5000"   # Render 배포 시: "https://your-backend.onrender.com"

# ─────────────────────────────────────────────
# 페이지 설정
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="KORMARC 653 필드 생성기",
    page_icon="📚",
    layout="centered",
)

# ─────────────────────────────────────────────
# 사이드바: API 키 입력
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("🔑 API 키 설정")
    ttb_key    = st.text_input("Aladin TTB Key",  type="password", placeholder="ttbxxxxxxxxxxxxxxxx")
    openai_key = st.text_input("OpenAI API Key",  type="password", placeholder="sk-...")
    st.markdown("---")
    st.caption(f"백엔드: `{BACKEND_URL}`")
    st.caption("알라딘 Open API: https://www.aladin.co.kr/ttb/wblog_guide.aspx")
    st.caption("OpenAI API: https://platform.openai.com/api-keys")


# ─────────────────────────────────────────────
# Session State 초기화
# ─────────────────────────────────────────────
_DEFAULTS: dict = {
    "meta_loaded":   False,
    "title":         "",
    "author":        "",
    "categoryName":  "",
    "description":   "",
    "toc":           "",
    "field_653":     "",
    "kw_list":       [],   # AI 원본 (읽기 전용)
    "edited_kw":     [],   # 사서 편집용 복사본
    "cot_reasoning": "",
    "last_isbn":     "",
    "confirmed":     False,
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ─────────────────────────────────────────────
# 백엔드 통신 헬퍼
# ─────────────────────────────────────────────
def _post(endpoint: str, payload: dict, timeout: int = 60) -> dict:
    """
    Flask 백엔드에 POST 요청을 보내고 JSON을 반환합니다.
    네트워크 오류 / 비200 응답은 {"error": "..."} 형태로 감싸 반환합니다.
    """
    url = f"{BACKEND_URL}{endpoint}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        data = resp.json()
        if resp.status_code != 200:
            return {"error": data.get("error", f"HTTP {resp.status_code}")}
        return data
    except requests.exceptions.ConnectionError:
        return {"error": f"백엔드 서버에 연결할 수 없습니다. ({url})"}
    except requests.exceptions.Timeout:
        return {"error": "백엔드 요청 시간이 초과되었습니다."}
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
# 헬퍼: 키워드 리스트 → 653 필드 재조립 (UI 전용)
# ─────────────────────────────────────────────
def _rebuild_field(kw_list: list[str]) -> str:
    valid = [kw.strip() for kw in kw_list if kw.strip()]
    if not valid:
        return ""
    subfields = "".join(f"$a{kw}" for kw in valid)
    return f"=653  \\\\{subfields}"


# ─────────────────────────────────────────────
# 메인 UI
# ─────────────────────────────────────────────
st.title("📚 KORMARC 653 필드 생성기")
st.caption("ISBN 조회 → 메타데이터 확인/수정 → AI 생성 → 사서 검수 → 최종 확정")
st.markdown("---")

# ══════════════════════════════════════════════
# 단계 1 · ISBN 입력 & 도서 정보 가져오기
# ══════════════════════════════════════════════
st.markdown("#### 1단계 · 도서 정보 가져오기")

isbn_col, fetch_col = st.columns([3, 1])
with isbn_col:
    isbn = st.text_input(
        "ISBN", placeholder="13자리 숫자 입력  예) 9788932041234",
        max_chars=13, label_visibility="collapsed",
    )
with fetch_col:
    fetch_btn = st.button("📥 정보 가져오기", use_container_width=True)

if fetch_btn:
    if not ttb_key:
        st.warning("사이드바에서 Aladin TTB Key를 입력하세요.")
    elif not isbn or not isbn.strip().isdigit() or len(isbn.strip()) != 13:
        st.warning("올바른 ISBN 13자리 숫자를 입력하세요.")
    else:
        with st.spinner("백엔드를 통해 알라딘 도서 정보를 가져오는 중..."):
            result = _post(
                "/api/fetch_meta",
                {"isbn": isbn.strip(), "ttb_key": ttb_key},
                timeout=20,
            )

        if "error" in result:
            st.error(result["error"])
        else:
            st.session_state.update({
                "title":        result.get("title", ""),
                "author":       result.get("author", ""),
                "categoryName": result.get("categoryName", ""),
                "description":  result.get("description", ""),
                "toc":          result.get("toc", ""),
                "meta_loaded":  True,
                "last_isbn":    isbn.strip(),
                "field_653":    "",
                "kw_list":      [],
                "edited_kw":    [],
                "cot_reasoning": "",
                "confirmed":    False,
            })
            st.success(f"✅ 불러오기 완료: **{result.get('title', '')}**")

# ══════════════════════════════════════════════
# 단계 2 · 메타데이터 확인 및 수정
# ══════════════════════════════════════════════
st.markdown("---")
st.markdown("#### 2단계 · 메타데이터 확인 및 수정")

if not st.session_state["meta_loaded"]:
    st.info("위에서 ISBN을 입력하고 **정보 가져오기**를 누르면 아래 입력창이 자동으로 채워집니다.")

col_l, col_r = st.columns(2)
with col_l:
    edit_title  = st.text_input("제목",  value=st.session_state["title"],  placeholder="도서 제목")
with col_r:
    edit_author = st.text_input("저자",  value=st.session_state["author"], placeholder="저자명")

edit_category    = st.text_input("카테고리", value=st.session_state["categoryName"], placeholder="카테고리")
edit_description = st.text_area(
    "초록 / 책 소개  ✏️ (직접 수정 가능)",
    value=st.session_state["description"], height=200,
    placeholder="책 소개 또는 초록을 입력하세요. 내용이 풍부할수록 주제어 품질이 높아집니다.",
)
edit_toc = st.text_area(
    "목차  ✏️ (직접 수정 가능)",
    value=st.session_state["toc"], height=150, placeholder="목차를 입력하세요.",
)

# ══════════════════════════════════════════════
# 단계 3 · AI 653 필드 생성
# ══════════════════════════════════════════════
st.markdown("---")
st.markdown("#### 3단계 · AI 653 필드 생성")

run_btn = st.button("🔖 653 필드 생성 (CoT)", type="primary", use_container_width=True)

if run_btn:
    if not openai_key:
        st.warning("사이드바에서 OpenAI API Key를 입력하세요.")
    elif not edit_title.strip():
        st.warning("제목이 비어 있습니다.")
    else:
        payload = {
            "openai_key":   openai_key,
            "title":        edit_title.strip(),
            "author":       edit_author.strip(),
            "categoryName": edit_category.strip(),
            "description":  edit_description.strip(),
            "toc":          edit_toc.strip(),
        }
        with st.spinner("백엔드에서 GPT-4o-mini가 단계별로 사고 중입니다..."):
            result = _post("/api/generate_653", payload, timeout=90)

        if "error" in result:
            st.error(result["error"])
        else:
            kw_list   = result.get("keywords", [])
            field_653 = result.get("field_653", "")
            reasoning = result.get("reasoning", "")

            st.session_state["field_653"]     = field_653
            st.session_state["kw_list"]       = kw_list
            st.session_state["edited_kw"]     = kw_list.copy()
            st.session_state["cot_reasoning"] = reasoning
            st.session_state["confirmed"]     = False

# ── AI 생성 결과 미리보기 ──
if st.session_state["field_653"]:
    st.success("✅ AI 생성 완료!")
    st.markdown("**AI 생성 원본 653 필드**")
    st.code(st.session_state["field_653"], language=None)

    if st.session_state["cot_reasoning"]:
        with st.expander("🧠 AI 사고 과정 보기 (Chain-of-Thought)", expanded=False):
            st.markdown(
                f"<div style='font-size:0.87rem;line-height:1.8;"
                f"background:#f8f9fa;padding:1rem;border-radius:6px;"
                f"border-left:4px solid #1a1a2e;white-space:pre-wrap;'>"
                f"{st.session_state['cot_reasoning']}"
                f"</div>",
                unsafe_allow_html=True,
            )

# ══════════════════════════════════════════════
# 단계 4 · Human-in-the-Loop 사서 검수
# ══════════════════════════════════════════════
if st.session_state.get("edited_kw") is not None and st.session_state["kw_list"]:

    st.markdown("---")
    st.markdown("#### 4단계 · 사서 검수 및 키워드 편집")
    st.caption("AI가 생성한 키워드를 직접 수정·삭제하거나 새 키워드를 추가할 수 있습니다.")

    # 삭제 처리
    if "delete_idx" in st.session_state:
        idx = st.session_state.pop("delete_idx")
        if 0 <= idx < len(st.session_state["edited_kw"]):
            st.session_state["edited_kw"].pop(idx)
            st.session_state["confirmed"] = False

    edited_kw = st.session_state["edited_kw"]

    # 편집 테이블 헤더
    st.markdown(
        "<div style='background:#f8f9fa;border-radius:8px;padding:12px 16px;"
        "border:1px solid #e0e0e0;margin-bottom:8px;'>"
        "<b style='font-size:0.85rem;color:#555;'>NO&nbsp;&nbsp;&nbsp;"
        "키워드 (직접 수정 가능)&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
        "삭제</b></div>",
        unsafe_allow_html=True,
    )

    updated_kw: list[str] = []
    for i, kw in enumerate(edited_kw):
        col_no, col_kw, col_del = st.columns([0.5, 5.5, 1])
        with col_no:
            st.markdown(
                f"<div style='padding-top:8px;font-size:0.85rem;"
                f"color:#888;text-align:center;'>{i + 1}</div>",
                unsafe_allow_html=True,
            )
        with col_kw:
            new_val = st.text_input(
                label=f"kw_{i}", value=kw,
                label_visibility="collapsed", key=f"kw_input_{i}",
            )
            updated_kw.append(new_val)
        with col_del:
            if st.button("🗑", key=f"del_{i}", help=f"'{kw}' 삭제"):
                st.session_state["delete_idx"] = i
                st.rerun()

    st.session_state["edited_kw"] = updated_kw

    # 키워드 추가
    st.markdown("")
    add_col, btn_col = st.columns([5, 1.5])
    with add_col:
        new_kw_input = st.text_input(
            "새 키워드 추가", placeholder="추가할 키워드 입력 후 버튼 클릭",
            label_visibility="collapsed", key="new_kw_input",
        )
    with btn_col:
        if st.button("➕ 추가", use_container_width=True):
            if new_kw_input.strip():
                st.session_state["edited_kw"].append(new_kw_input.strip())
                st.session_state["confirmed"] = False
                st.rerun()
            else:
                st.warning("추가할 키워드를 입력하세요.")

    st.markdown("")

    # 실시간 미리보기
    live_field = _rebuild_field(st.session_state["edited_kw"])

    st.markdown("**✏️ 실시간 수정 결과 미리보기**")
    if live_field:
        if live_field != st.session_state["field_653"]:
            st.markdown(
                "<span style='font-size:0.78rem;color:#c0392b;font-weight:600;'>"
                "⚠ AI 원본에서 수정됨</span>",
                unsafe_allow_html=True,
            )
        st.code(live_field, language=None)

        valid_kws = [kw.strip() for kw in st.session_state["edited_kw"] if kw.strip()]
        if valid_kws:
            tag_html = " ".join(
                f"<span style='background:#2c7a7b;color:#fff;"
                f"padding:4px 11px;border-radius:3px;font-size:0.85rem;"
                f"margin:2px;display:inline-block;'>{kw}</span>"
                for kw in valid_kws
            )
            st.markdown(tag_html, unsafe_allow_html=True)
    else:
        st.warning("유효한 키워드가 없습니다. 키워드를 추가하세요.")

    st.markdown("")

    # ══════════════════════════════════════════
    # 단계 5 · 최종 확정
    # ══════════════════════════════════════════
    st.markdown("---")
    st.markdown("#### 5단계 · 최종 확정")

    confirm_btn = st.button(
        "✅ 최종 확정 및 복사 준비",
        type="primary",
        use_container_width=True,
        disabled=(not live_field),
    )

    if confirm_btn:
        st.session_state["confirmed"] = True
        st.session_state["field_653"] = live_field

    if st.session_state["confirmed"] and st.session_state["field_653"]:
        final_field = st.session_state["field_653"]

        st.success("🎉 최종 KORMARC 653 필드가 확정되었습니다!")
        st.markdown("**📋 확정된 KORMARC 653 필드**")
        st.code(final_field, language=None)

        escaped = final_field.replace("\\", "\\\\").replace("`", "\\`").replace("'", "\\'")
        copy_js = f"""
        <button onclick="
            navigator.clipboard.writeText('{escaped}')
                .then(() => {{
                    this.innerText = '✅ 복사 완료!';
                    this.style.background = '#27ae60';
                    setTimeout(() => {{
                        this.innerText = '📋 클립보드 복사';
                        this.style.background = '#1a1a2e';
                    }}, 2000);
                }})
                .catch(() => alert('복사 실패: 수동으로 위 텍스트를 복사하세요.'));
        "
        style="
            background:#1a1a2e; color:#fff;
            border:none; border-radius:6px;
            padding:10px 24px; font-size:0.9rem;
            cursor:pointer; width:100%;
            transition: background 0.2s ease;
        ">📋 클립보드 복사</button>
        """
        st.markdown(copy_js, unsafe_allow_html=True)

        st.markdown("")
        final_kws = [kw.strip() for kw in st.session_state["edited_kw"] if kw.strip()]
        if final_kws:
            st.markdown(f"**확정 주제어 {len(final_kws)}개**")
            summary_html = " ".join(
                f"<span style='background:#1a1a2e;color:#f5f0e8;"
                f"padding:5px 12px;border-radius:3px;font-size:0.88rem;"
                f"margin:2px;display:inline-block;'>{kw}</span>"
                for kw in final_kws
            )
            st.markdown(summary_html, unsafe_allow_html=True)
