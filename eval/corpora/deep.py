"""wide 와 같은 내용을 실제 문서 크기의 쪽에 담은 코퍼스.

**왜 또 만드나.** CAND_K 를 정할 때 쓴 wide 는 한 쪽이 53자다. 그런데 이
시스템이 실제로 다루는 문서는 훨씬 두껍다 — 이 저장소 DB 에 들어 있는
인공지능기본법은 청크 중앙값이 1,295자다. 리랭킹 비용은 후보 **개수**가
아니라 입력 **총길이**에 지배되므로 둘은 다른 이야기다. 2026-09-11 실측:

    같은 CAND_K=20 에서    53자 청크 →   474ms
                        1,295자 청크 → 9,409ms

즉 wide 로 잰 "CAND_K=20 에서 R@1 1.000, 474ms" 는 실제 사용을 설명하지
못한다. 이 저장소는 같은 함정에 이미 한 번 빠졌다 — RERANK_MAX_CHARS
실험이 짧은 픽스처 탓에 "품질 변화 없음" 으로 나왔던 건(longchunk 가 그
교훈으로 태어났다). longchunk 는 길이는 맞지만 12쪽뿐이라 CAND_K 가 아예
걸리지 않는다. **길이와 폭을 동시에 만족하는 코퍼스가 없었다.**

**설계.** wide 의 ``CLAUSES`` 와 ``QUERIES`` 를 그대로 쓰고 쪽 길이만
키운다. 조항도 질의도 정답 쪽 번호도 전부 같으므로, wide 와의 차이는
**길이 하나로만** 귀속된다 — 통제된 비교다.

사실 문장은 쪽마다 다른 깊이에 놓는다(앞/중간/뒤를 순환). longchunk 는
깊이만 변주해 그 효과를 격리하는 코퍼스이고, 이쪽은 실제 문서처럼 깊이가
섞여 있는 쪽이 목적이다. 채움 문장은 어떤 질의에도 답하지 않는다 —
쪽을 두껍게 만들어 사실을 희석하는 것이 유일한 역할이다.

주의: 채움은 임베딩도 바꾼다. 53자 조항 하나로 이뤄진 쪽과, 같은 조항이
1,200자 속에 묻힌 쪽은 다른 벡터가 된다. 그래서 이 코퍼스는 리랭킹뿐
아니라 **검색이 정답 쪽을 후보에 올리는지**까지 함께 시험한다. 실제
사용이 그렇기 때문에 의도된 것이다.
"""

from eval.corpora import wide

# longchunk 의 채움 문장을 그대로 쓴다. 같은 목적(사실을 희석하지 않으면서
# 쪽을 채우기)이고, 두 코퍼스가 같은 재료를 쓰는 편이 비교에 낫다.
from eval.corpora.longchunk import _FILLER

# 실제 문서(인공지능기본법)의 청크 중앙값 1,295자에 맞춘다. CHUNK_SIZE 는
# 700토큰이고 한국어는 토큰당 대략 2자 안팎이라, 이 길이면 한 쪽이 대체로
# 한 청크가 된다.
_PAGE_CHARS = 1250

# 사실을 놓을 깊이. 쪽마다 순환시켜 실제 문서처럼 섞는다.
_DEPTHS = (0, 400, 850)


def _filler_run(seed: int, min_chars: int) -> str:
    """``min_chars`` 이상이 될 때까지 채움 문장을 잇는다.

    쪽마다 다른 지점에서 시작해 모든 쪽이 같은 문단으로 시작하지 않게
    한다 — 그러면 검색이 내용이 아니라 공통 접두사를 보고 고를 수 있다.
    """
    out: list[str] = []
    total = 0
    i = seed
    while total < min_chars:
        s = _FILLER[i % len(_FILLER)]
        out.append(s)
        total += len(s) + 1
        i += 1
    return " ".join(out)


def _build() -> list[str]:
    pages: list[str] = []
    for index, clause in enumerate(wide.CLAUSES):
        depth = _DEPTHS[index % len(_DEPTHS)]
        head = _filler_run(index, depth) if depth else ""
        tail_chars = max(0, _PAGE_CHARS - depth - len(clause))
        tail = _filler_run(index + 7, tail_chars) if tail_chars else ""
        pages.append(" ".join(part for part in (head, clause, tail) if part))
    return pages


CLAUSES = _build()

# 질의·정답 쪽은 wide 와 완전히 같다. 길이만 다른 통제 비교라는 것이
# 이 코퍼스의 요점이므로, 여기서 바꾸면 의미가 사라진다.
QUERIES = wide.QUERIES

# 길이를 키우면서 조항 본문이 잘리거나 섞이지 않았는지 확인한다. 이 검사가
# 없으면 "정답 쪽에 정답이 없는" 코퍼스로 조용히 측정하게 된다.
for _i, _clause in enumerate(wide.CLAUSES):
    assert _clause in CLAUSES[_i], f"page {_i + 1} lost its clause"
assert len(CLAUSES) == len(wide.CLAUSES)
