"""End-to-end smoke test across M1 + M2 + M3, driven through the real HTTP API.

Signs up two real users and walks the whole product: upload -> indexing ->
hybrid retrieval -> cited answer -> refusal -> tenant isolation -> session
lifecycle. Each user's document carries a unique value so any cross-tenant leak
shows up immediately.

This is a manual smoke test, not part of ``pytest``: it needs a running server
plus Postgres, the embedding/rerank server, and an LLM key. Checks that require
the embedding server are reported SKIP when it is unreachable, so the
auth/isolation half still runs without it.

    uvicorn app.main:app &          # server under test
    python -m scripts.e2e_smoke     # or --base http://host:port

Exits non-zero if any check fails.
"""

import argparse
import sys
import time
import uuid

import httpx
import pymupdf

from app.config import settings

RESULTS: list[tuple[str, str, str]] = []

# Unique per-tenant values: if isolation breaks, the other tenant's number
# shows up in an answer or citation snippet.
ALICE_ONLY = "150,000"
BOB_ONLY = "300,000"

ALICE_CLAUSES = [
    "제1조 (보증금) 임대차 보증금은 금 오천만원(50,000,000원)으로 한다.",
    "제2조 (계약기간) 계약 기간은 2024년 3월 1일부터 2026년 2월 28일까지 2년으로 한다.",
    "제3조 (관리비) 월 관리비는 십오만원(150,000원)이며 매월 25일에 납부한다.",
]
BOB_CLAUSES = [
    "제1조 (자기부담금) 보험 자기부담금은 금 삼십만원(300,000원)으로 한다.",
    "제2조 (보장한도) 연간 보장 한도는 오천만원(50,000,000원)이다.",
    "제3조 (면책기간) 가입 후 90일간은 보장이 개시되지 않는다.",
]


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(("PASS" if ok else "FAIL", name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    RESULTS.append(("SKIP", name, why))
    print(f"  [SKIP] {name} — {why}")


def make_pdf(path: str, clauses: list[str]) -> None:
    doc = pymupdf.open()
    try:
        for text in clauses:
            page = doc.new_page()
            rect = pymupdf.Rect(60, 80, page.rect.width - 60, page.rect.height - 80)
            page.insert_textbox(rect, text, fontsize=13, fontname="korea")
        doc.save(path)
    finally:
        doc.close()


def embed_server_up() -> bool:
    try:
        url = settings.tei_url.rstrip("/") + "/health"
        return httpx.get(url, timeout=10).status_code == 200
    except Exception:
        return False


def wait_status(client: httpx.Client, doc_id: str, timeout_s: int = 180) -> dict:
    deadline = time.time() + timeout_s
    seen: list[str] = []
    while time.time() < deadline:
        body = client.get(f"/documents/{doc_id}").json()
        if not seen or seen[-1] != body["status"]:
            seen.append(body["status"])
        if body["status"] in ("ready", "failed"):
            body["_transitions"] = seen
            return body
        time.sleep(1)
    return {"status": "timeout", "_transitions": seen}


def run(base: str) -> int:
    have_embed = embed_server_up()
    print(f"embedding/rerank server: {'UP' if have_embed else 'DOWN (일부 SKIP)'}\n")

    a_email = f"alice-{uuid.uuid4().hex[:8]}@example.com"
    b_email = f"bob-{uuid.uuid4().hex[:8]}@example.com"
    a_pdf, b_pdf, bad_pdf = "/tmp/smoke_alice.pdf", "/tmp/smoke_bob.pdf", "/tmp/smoke_bad.pdf"
    make_pdf(a_pdf, ALICE_CLAUSES)
    make_pdf(b_pdf, BOB_CLAUSES)
    with open(bad_pdf, "wb") as fh:
        fh.write(b"%PDF-1.4 this is not a real pdf")

    anon = httpx.Client(base_url=base, timeout=180)
    alice = httpx.Client(base_url=base, timeout=180)
    bob = httpx.Client(base_url=base, timeout=180)

    print("── 인증 (M3) ──")
    check("비인증 /documents → 401", anon.get("/documents").status_code == 401)
    check("비인증 /query → 401",
          anon.post("/query", json={"question": "x"}).status_code == 401)
    check("/health 는 공개", anon.get("/health").status_code == 200)
    check("A 가입 → 201",
          alice.post("/auth/signup",
                     json={"email": a_email, "password": "password123"}).status_code == 201)
    check("가입 후 /auth/me 동일 계정",
          alice.get("/auth/me").json().get("email") == a_email)
    check("중복 가입 → 409",
          anon.post("/auth/signup",
                    json={"email": a_email, "password": "password123"}).status_code == 409)
    check("틀린 비밀번호 로그인 → 401",
          anon.post("/auth/login",
                    json={"email": a_email, "password": "wrong-password"}).status_code == 401)
    check("B 가입 → 201",
          bob.post("/auth/signup",
                   json={"email": b_email, "password": "password123"}).status_code == 201)

    print("\n── 인덱싱 (M1) ──")
    a_doc = None
    if have_embed:
        with open(a_pdf, "rb") as fh:
            up = alice.post("/documents", files={"file": ("alice.pdf", fh, "application/pdf")})
        check("A 업로드 → 202", up.status_code == 202, f"HTTP {up.status_code}")
        a_doc = up.json()["id"]
        st = wait_status(alice, a_doc)
        check("A 인덱싱 ready", st["status"] == "ready", f"transitions={st.get('_transitions')}")
        check("num_pages 정확", st.get("num_pages") == len(ALICE_CLAUSES))
    else:
        skip("A 업로드/인덱싱", "임베딩 서버 없음")
        skip("num_pages 확인", "임베딩 서버 없음")

    with open(bad_pdf, "rb") as fh:
        bad = alice.post("/documents", files={"file": ("broken.pdf", fh, "application/pdf")})
    bad_id = bad.json()["id"]
    bst = wait_status(alice, bad_id, timeout_s=60)
    check("손상 PDF → failed + error",
          bst["status"] == "failed" and bool(bst.get("error")), str(bst.get("error"))[:60])
    check("실패 후에도 서버 생존", anon.get("/health").status_code == 200)

    print("\n── 질의·인용·거부 (M1/M2) ──")
    if have_embed and a_doc:
        r = alice.post("/query", json={"question": "관리비는 얼마이고 언제 납부하나요?",
                                       "document_id": a_doc}).json()
        cites = [c["page_from"] for c in r["citations"]]
        check("답변 가능 질의 → 정답+인용",
              not r["refused"] and ALICE_ONLY in r["answer"], f"cites={cites}")
        check("인용 페이지가 실제 위치(p3)", 3 in cites, f"cites={cites}")
        r2 = alice.post("/query", json={"question": "반려동물을 키울 수 있나요?",
                                        "document_id": a_doc}).json()
        check("문서에 없는 질의 → refused", r2["refused"] is True, r2["answer"][:40])
        r3 = alice.post("/query", json={"question": "관리비는 얼마인가요?",
                                        "document_id": a_doc, "hybrid": False}).json()
        check("dense-only(M1 경로)도 동작", not r3["refused"], r3["answer"][:40])
    else:
        for name in ("답변 가능 질의 → 정답+인용", "인용 페이지가 실제 위치(p3)",
                     "문서에 없는 질의 → refused", "dense-only(M1 경로)도 동작"):
            skip(name, "임베딩 서버 없음")

    print("\n── 테넌트 격리 (M3) ──")
    b_doc = None
    if have_embed:
        with open(b_pdf, "rb") as fh:
            up = bob.post("/documents", files={"file": ("bob.pdf", fh, "application/pdf")})
        b_doc = up.json()["id"]
        check("B 인덱싱 ready", wait_status(bob, b_doc)["status"] == "ready")
    else:
        skip("B 업로드/인덱싱", "임베딩 서버 없음")

    if a_doc:
        check("B가 A 문서 조회 → 404", bob.get(f"/documents/{a_doc}").status_code == 404)
        check("B가 A 문서 삭제 → 404", bob.delete(f"/documents/{a_doc}").status_code == 404)
        check("삭제 시도 후에도 A 문서 생존",
              alice.get(f"/documents/{a_doc}").status_code == 200)
    else:
        for name in ("B가 A 문서 조회 → 404", "B가 A 문서 삭제 → 404",
                     "삭제 시도 후에도 A 문서 생존"):
            skip(name, "A 문서 없음(임베딩 서버 없음)")

    b_list = [d["filename"] for d in bob.get("/documents").json()]
    check("B 목록에 A 문서 없음", "alice.pdf" not in b_list, f"B sees {b_list}")
    a_list = [d["filename"] for d in alice.get("/documents").json()]
    check("A 목록에 B 문서 없음", "bob.pdf" not in a_list, f"A sees {a_list}")

    if have_embed and a_doc and b_doc:
        # An unowned scope is rejected up front, indistinguishable from a
        # document that never existed.
        resp = bob.post("/query", json={"question": "관리비는 얼마인가요?",
                                        "document_id": a_doc})
        check("B가 A의 document_id로 질의 → 404(존재 은닉)",
              resp.status_code == 404 and ALICE_ONLY not in resp.text,
              f"HTTP {resp.status_code}")
        r = bob.post("/query", json={"question": "자기부담금은 얼마인가요?"}).json()
        leaked = ALICE_ONLY in r["answer"] or any(
            ALICE_ONLY in c["snippet"] for c in r["citations"])
        check("B의 전체 질의에 A 내용 미혼입", not leaked, r["answer"][:40])
        check("B는 자기 문서로 정답", BOB_ONLY in r["answer"], r["answer"][:40])
    else:
        for name in ("B가 A의 document_id로 질의 → 거부(내용 미노출)",
                     "B의 전체 질의에 A 내용 미혼입", "B는 자기 문서로 정답"):
            skip(name, "임베딩 서버 없음")

    print("\n── 세션 수명주기 (M3) ──")
    alice.post("/auth/logout")
    check("로그아웃 후 /documents → 401", alice.get("/documents").status_code == 401)
    check("재로그인 성공",
          alice.post("/auth/login",
                     json={"email": a_email, "password": "password123"}).status_code == 200)
    check("재로그인 후 접근 복구", alice.get("/documents").status_code == 200)

    for client, doc in ((alice, a_doc), (bob, b_doc), (alice, bad_id)):
        if doc:
            client.delete(f"/documents/{doc}")

    passed = sum(1 for s, _, _ in RESULTS if s == "PASS")
    failed = sum(1 for s, _, _ in RESULTS if s == "FAIL")
    skipped = sum(1 for s, _, _ in RESULTS if s == "SKIP")
    print("\n" + "=" * 60)
    print(f"PASS {passed}  FAIL {failed}  SKIP {skipped}")
    if failed:
        print("\n실패 항목:")
        for status, name, detail in RESULTS:
            if status == "FAIL":
                print(f"  - {name} ({detail})")
    print("\n참고: 테스트 계정은 남습니다. 정리하려면 해당 users 행을 삭제하세요"
          " (documents/chunks는 cascade).")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    return run(parser.parse_args().base)


if __name__ == "__main__":
    sys.exit(main())
