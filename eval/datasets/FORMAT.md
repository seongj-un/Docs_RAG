# 골든셋 jsonl 포맷 (M7 L1)

한 줄에 질문 하나. 파일 하나가 데이터셋 하나이고, 이름은 파일 stem,
버전은 **파일 바이트의 sha256** 이다. 두 실행을 비교할 때 이 해시가 다르면
`eval.harness diff` 는 비교를 거부한다 — 질문이 달라진 것을 검색 품질 변화로
읽는 순간 회귀 경보는 거짓말이 되기 때문이다.

파서는 `eval/datasets/__init__.py`, 검사는 `python -m eval.harness validate`.

## 한 줄 예시

```json
{"id": "syn-golden-001", "question": "임대차 보증금은 얼마인가요?", "type": "single_fact", "gold_spans": [{"doc": "lease", "snippet": "임대차 보증금은 금 오천만원(50,000,000원)으로 한다."}], "reference_answer": "임대차 보증금은 오천만원(50,000,000원)이다.", "difficulty": "easy", "source": "eval/corpora/golden.py — M4 합성 골든셋(임대차·보험·SaaS) 변환", "split": "tune", "verified_at": "2026-09-14"}
```

## 필드

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | string | 파일 안에서 유일. 실행 기록·diff 리포트가 이 값으로 문항을 부른다 |
| `question` | string | 실제 말투 그대로. 문서 문장을 그대로 베끼지 않는다 |
| `type` | enum | `single_fact` · `multi_doc` · `rule_exception` · `no_answer` · `freshness` |
| `gold_spans` | array | 정답 근거 **원문 스니펫**. `{doc, snippet}` 의 배열. `no_answer` 는 빈 배열 |
| `reference_answer` | string | L2 judge 용 기준 답변. L1 은 읽지 않는다 |
| `difficulty` | enum | `easy` · `medium` · `hard` |
| `source` | string | 어디서 나온 질문인지. "지어낸 질문이 아니다"의 근거 |
| `split` | enum | `tune` 70% / `holdout` 30%. holdout 은 W8 최종 측정에서만 연다 |
| `verified_at` | string | 정답을 확인한 날짜. `freshness` 유형에는 필수 |

`type` 은 별칭을 받는다: `unanswerable` → `no_answer`, `recency` → `freshness`.
M7 개요 표와 W1 설계 노트가 같은 유형을 다르게 부르기 때문이고, 로더가 한쪽
이름으로 접어 넣는다.

## 정답을 청크 ID 로 적지 않는 이유

청크 UUID 는 두 가지 이유로 골든셋에 담을 수 없다.

1. 합성 코퍼스는 인덱싱할 때마다 UUID 가 새로 생긴다. 한 번 인덱싱한 뒤에는
   데이터셋이 죽은 참조만 들고 있게 된다.
2. 더 중요한 쪽 — **W5 가 청킹 전략을 바꾸는 순간 골든셋 전체가 무효가 된다.**
   청킹은 M7 의 주요 실험 대상이다. 평가 대상을 바꾸면 평가 기준이 함께
   무너지는 설계는 처음부터 성립하지 않는다.

그래서 저장하는 것은 **원문 스니펫**이고, 실행 시점에 `eval/gold.py` 가 그
스니펫을 지금 인덱스의 청크로 역매칭한다. 청킹이 어떻게 바뀌든 스니펫은 문서에
그대로 남아 있으므로 종속성이 생기지 않는다.

### 스니펫 규칙

- 길이 **20~80자**(공백 정규화 후). 짧으면 여러 곳에 걸리고, 길면 청크 경계를
  쉽게 넘는다.
- 한 문서 안에서 **3곳 이하**로 매칭돼야 한다. 그보다 많으면 정답을 지목하지
  못하므로 스니펫 쪽을 좁힌다.
- 매칭은 공백·개행을 정규화한 뒤 부분 문자열 비교. PDF 추출기가 같은 문장을
  줄바꿈만 다르게 내놓아도 결과가 갈리면 안 된다.
- 스니펫이 청크 경계에 걸치면 **겹치는 청크를 전부** 정답으로 친다. 문자 위치를
  계산해서 판단한다 — "스니펫을 통째로 포함하는 청크"만 찾으면 경계를 넘은
  스니펫은 정답이 0개가 되고, 멀쩡한 검색이 실패로 기록된다.
- 매칭이 **0건이면 러너가 죽는다.** 문서가 바뀌었는데 골든셋이 따라가지
  못했다는 신호다. 고치지 말고 CI 에서 깨지게 두는 것이 W1 의 결정이다 —
  조용히 넘기면 그 문항은 영원히 0점으로 평균을 갉아먹는다.

## L1 채점 방식

- **recall@k** — 정답 **스팬** 중 상위 k개 청크로 덮인 비율(스팬 단위 macro).
  청크 단위로 세지 않는 이유는 한 스팬이 네 청크로 쪼개졌다는 이유만으로
  가중치가 커지면 안 되기 때문이다 — 그건 청킹의 부산물이고, 그 청킹이 바로
  W5 에서 바뀐다.
- **MRR** — 정답 청크가 처음 등장하는 순위의 역수.
- **nDCG@10** — 새 스팬을 덮는 첫 청크에만 이득 1. 이미 덮은 스팬을 덮는 두
  번째 청크는 0. IDCG 는 서로 다른 스팬 `min(n, k)` 개를 이상적으로 늘어놓은 값.
- **pageR@k** — 쪽 단위 recall 도 같이 남긴다. W1 베이스라인(Notion MCP)은
  청크가 아니라 페이지/블록을 돌려주므로, 청크 recall 로만 비교하면 우리 쪽이
  유리하게 나온다. 문서가 여럿이면 쪽은 `(문서, 쪽번호)` 쌍으로 센다.
- `no_answer` 문항은 **L1 에서 제외**한다. 정답 청크가 없으므로 잴 것이 없고,
  거부 정확도는 L2 judge 의 몫이다.
- 지표는 항상 **유형별·split별로도** 쪼개서 본다. 전체 평균만 보면 `multi_doc`
  실패가 `single_fact` 성공에 가려진다.

## 문서 provider

`gold_spans[].doc` 은 이름일 뿐이다. 이름을 실제 쪽 텍스트로 바꾸는 것은
provider 모듈이고, `--provider` 로 지정한다(기본
`eval.datasets.synthetic`). provider 는 `names()` 와 `pages_for(doc)` 두 개만
노출하면 된다.

이 이음매 덕분에 **업무 문서 골든셋은 private repo 에 두고 하네스·포맷·지표는
공개**할 수 있다 — M7 의 공개/비공개 원칙이 코드에서 갈리는 지점이다.

## 이 저장소에 들어 있는 데이터셋

| 파일 | 문항 | 문서 | 무엇을 위한 것인가 |
| --- | --- | --- | --- |
| `synthetic_golden.jsonl` | 36 | lease · insurance · saas | 여러 문서에 걸친 하네스 검증. `multi_doc`(다중 스팬)·`no_answer` 경로가 여기에만 있다 |
| `synthetic_hard.jsonl` | 20 | fees (혼동 요금 조항 40쪽) | 거의 같은 조항 40개 속에서 하나를 집어내는지 |

둘 다 **합성 코퍼스에서 기계 변환한 것**이다(`build_synthetic.py`). 하네스가
제대로 도는지를 검증하기 위한 픽스처이지, M7 의 측정 대상이 아니다. 진짜
측정은 업무 문서 골든셋 60~80문항으로 하고, 그건 private repo 에 있다.

**알려진 한계 — 유형 분포가 W1 목표와 다르다.**

| 유형 | W1 목표 | `synthetic_golden` |
| --- | --- | --- |
| `single_fact` | 30% | 61% |
| `multi_doc` | 25% | 11% |
| `rule_exception` | 20% | 19% |
| `no_answer` | 15% | 8% |
| `freshness` | 10% | **0%** |

`freshness` 가 0인 것은 채우다 만 것이 아니라 **채울 수 없어서**다. 합성
코퍼스에는 개정 이력도 버전도 없어서 "오래된 값을 물고 오는가"를 잴 대상 자체가
없다. 없는 것을 있는 척 채우면 그 문항들이 freshness 를 측정한다고 거짓으로
보고하게 된다. 또 `synthetic_golden` 의 `multi_doc` 은 엄밀히는 **한 문서 안의
두 조항**을 합치는 문항이다(M4 의 `multi_hop`). L1 에서 요구하는 계산은
동일하지만(다중 스팬 macro recall·nDCG), 문서 간 혼동 자체는 업무 문서
골든셋이 들어와야 측정된다.

## 새 데이터셋을 만들 때

1. jsonl 을 쓰고, 문서 provider 를 준비한다.
2. `python -m eval.harness validate --dataset <path> --provider <module>` —
   포맷·스니펫 고유성·유형 분포가 한 번에 나온다.
3. `pytest tests/test_eval_dataset.py` — 저장소에 커밋한 데이터셋은 모든
   커밋에서 이 검사를 받는다.
