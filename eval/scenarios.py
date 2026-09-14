"""The L3 scenario set: what an agent *should do*, not what it should retrieve.

L1 (``eval/harness.py``) asks "did retrieval find the right chunk". L3 asks a
different question — **given this utterance and this tool surface, did the agent
behave the way a user would want?** — so it needs its own labels and its own
file. Mixing the two would be worse than duplication: the golden set's labels
are chunk snippets, which say nothing about whether calling a tool was the right
move at all.

Format is Notion W4's, widened where the narrow version could not express a
correct behaviour. One line per scenario, the dataset version is the sha256 of
the file's bytes (same convention as ``eval/datasets/FORMAT.md``).

## Fields

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `id` | string | 파일 안에서 유일 |
| `utterance` | string | 사용자가 실제로 칠 법한 말 |
| `history` | array | 이 발화 **앞의** 대화. `{role: user\\|model, text}` |
| `expected_tool` | enum | 역할: `search` · `fetch` · `list` · `none` |
| `fallback_tool` | enum | 그 역할의 툴이 이 조건에 **없을 때**의 정답 역할 |
| `expected_args` | object | 인자 채점 제약. 아래 참조 |
| `min_calls` / `max_calls` | int | 정당한 호출 수의 하한·상한 |
| `kind` | string | 시나리오 유형(보고서의 분포 표가 이걸로 묶는다) |
| `why` | string | **이 라벨이 정답인 이유.** 아래 참조 |

### `expected_tool` 이 역할인 이유

같은 일을 하는 툴의 이름이 조건마다 다르다(``search_documents`` vs
``search``). 이름으로 라벨링하면 리네임 하나가 "행동이 바뀌었다"로 집계된다.
역할 -> 이름 해석은 ``app/mcp/variants.ToolVariant.role_map()`` 이 한다.

### `fallback_tool` 이 필요한 이유

분해 조건에만 ``list_collections`` 가 있다. "내가 올린 문서 뭐뭐 있어?" 에 대해
**사용자 입장에서** 옳은 행동은, 그 툴이 있으면 그것을 부르는 것이고, 없으면
**아무것도 부르지 않고 못 한다고 말하는 것**이다 — 서버 안내문이 실제로 그렇게
적혀 있다("This server cannot list ... use the web application"). 검색 툴로
문서 목록을 흉내 내는 것은 둘 중 어느 쪽도 아니다. 그래서 정답은 조건마다
다르고, 그 차이가 곧 "분해하면 선택 정확도가 오르는가"의 답이다.

### `why` 가 필수인 이유 — Notion 함정 3

> 기대 툴을 미리 라벨링할 때 **본인 구현을 정답으로 가정하지 말 것.**
> 사용자 입장에서 맞는 행동을 정답으로 둔다.

라벨을 적을 때마다 "왜 이게 사용자 입장에서 맞는가"를 한 문장으로 쓰게 하면,
"우리 툴이 그렇게 생겼으니까"가 이유인 문항은 그 자리에서 드러난다. 로더가 이
필드를 비워 두지 못하게 막는다.

## 인자 제약 (`expected_args`)

| 키 | 의미 |
| --- | --- |
| `query_contains` | 질의 인자에 **전부** 들어 있어야 한다 |
| `query_not_contains` | 하나라도 들어 있으면 오답 |
| `args_exact` | 그 인자가 정확히 이 값이어야 한다 |
| `args_absent` | 그 인자가 설정돼 있으면 오답 |
| `any_call_contains` | 각 묶음이 **어느 한 호출**에서든 만족되면 된다 |

채점 규칙과 그 근거는 ``eval/l3.py`` 의 ``score_arguments`` 에 있다.

## 실행 시점 자리표시자

정답이 UUID 인 문항이 있다(예: "그 문서 안에서만 찾아줘"). UUID 는 인덱싱할
때마다 새로 생기므로 파일에 적을 수 없다 — 골든셋이 청크 ID 를 안 적는 것과
같은 이유다. 대신 자리표시자를 적고 실행 시점에 치환한다:

    {{doc:spec_errors.pdf}}          그 문서의 document_id
    {{chunk:spec_errors.pdf:2}}      그 문서의 2번 청크의 chunk_id

``history`` 의 본문과 ``expected_args`` 의 값 양쪽에서 쓸 수 있다.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from app.mcp.variants import ROLE_NONE, ROLES

DEFAULT_PATH = Path(__file__).with_name("scenarios_l3.jsonl")

# {{doc:name}} / {{chunk:name:index}}
_PLACEHOLDER = re.compile(r"\{\{(doc|chunk):([^:}]+)(?::(\d+))?\}\}")

_ARG_KEYS = frozenset(
    {
        "query_contains",
        "query_not_contains",
        "args_exact",
        "args_absent",
        "any_call_contains",
    }
)


@dataclass(frozen=True)
class Turn:
    role: str  # "user" | "model"
    text: str


@dataclass(frozen=True)
class Scenario:
    id: str
    utterance: str
    expected_tool: str
    kind: str
    why: str
    max_calls: int
    min_calls: int = 1
    fallback_tool: str = ROLE_NONE
    history: tuple[Turn, ...] = ()
    expected_args: dict = field(default_factory=dict)

    def expected_role_for(self, role_map: dict[str, str]) -> str:
        """The role that is correct under a tool surface offering ``role_map``.

        ``none`` 은 언제나 자기 자신이다 — 툴이 없어서가 아니라 부르지 않는 것이
        정답이라서 none 이므로, 폴백이 끼어들 자리가 없다.
        """
        if self.expected_tool == ROLE_NONE or self.expected_tool in role_map:
            return self.expected_tool
        return self.fallback_tool


def normalize(text: str) -> str:
    """Fold a string to the form substring checks run on.

    NFKC + 소문자 + 공백 제거. 공백까지 지우는 이유는 한국어 질의에서 띄어쓰기가
    흔들리기 때문이다("대기 순번" / "대기순번"). 토큰 단위로 자르지 않는 이유는
    조사다 — "학생의" 안의 "학생" 은 우리가 원하는 매칭이고, 토큰 동일성은 그것을
    놓친다.
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).lower()


def _turns(raw: list | None, sid: str) -> tuple[Turn, ...]:
    out = []
    for i, item in enumerate(raw or []):
        role = item.get("role")
        if role not in ("user", "model"):
            raise ValueError(f"{sid}: history[{i}].role 은 user 또는 model 이어야 한다")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{sid}: history[{i}].text 가 비었다")
        out.append(Turn(role=role, text=text))
    return tuple(out)


def _validate_args(spec: dict, sid: str) -> dict:
    unknown = set(spec) - _ARG_KEYS
    if unknown:
        raise ValueError(f"{sid}: 모르는 expected_args 키 {sorted(unknown)}")
    for key in ("query_contains", "query_not_contains", "args_absent"):
        if key in spec and not (
            isinstance(spec[key], list) and all(isinstance(v, str) for v in spec[key])
        ):
            raise ValueError(f"{sid}: expected_args.{key} 는 문자열 배열이어야 한다")
    if "any_call_contains" in spec:
        groups = spec["any_call_contains"]
        ok = isinstance(groups, list) and all(
            isinstance(g, list) and g and all(isinstance(v, str) for v in g)
            for g in groups
        )
        if not ok:
            raise ValueError(
                f"{sid}: expected_args.any_call_contains 는 비지 않은 배열의 배열"
            )
    for key in ("args_exact",):
        if key in spec and not isinstance(spec[key], dict):
            raise ValueError(f"{sid}: expected_args.{key} 는 객체여야 한다")
    return spec


def parse(line: str) -> Scenario:
    raw = json.loads(line)
    sid = raw.get("id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("id 가 없다")

    for required in ("utterance", "expected_tool", "kind", "why", "max_calls"):
        if required not in raw:
            raise ValueError(f"{sid}: {required} 가 없다")

    role = raw["expected_tool"]
    fallback = raw.get("fallback_tool", ROLE_NONE)
    for name, value in (("expected_tool", role), ("fallback_tool", fallback)):
        if value not in ROLES:
            raise ValueError(
                f"{sid}: {name}={value!r} 은 역할이 아니다 ({sorted(ROLES)})"
            )

    why = raw["why"]
    if not isinstance(why, str) or len(why.strip()) < 10:
        # 함정 3. 한 문장도 못 쓰겠으면 그 라벨은 아직 근거가 없다.
        raise ValueError(f"{sid}: why 가 비었거나 너무 짧다 — 라벨의 근거를 적을 것")

    min_calls = raw.get("min_calls", 0 if role == ROLE_NONE else 1)
    max_calls = raw["max_calls"]
    if not isinstance(max_calls, int) or max_calls < 0:
        raise ValueError(f"{sid}: max_calls 는 0 이상의 정수")
    if not isinstance(min_calls, int) or not (0 <= min_calls <= max_calls):
        raise ValueError(f"{sid}: min_calls 는 0..max_calls 여야 한다")
    if role == ROLE_NONE and max_calls != 0:
        # 부르지 않는 것이 정답인데 예산이 남아 있으면 "불필요 호출"이 정의되지
        # 않는다. 이 문항들이 지표의 절반이므로 여기서 막는다.
        raise ValueError(f"{sid}: expected_tool=none 이면 max_calls 도 0 이어야 한다")

    return Scenario(
        id=sid,
        utterance=raw["utterance"],
        expected_tool=role,
        fallback_tool=fallback,
        kind=raw["kind"],
        why=why,
        min_calls=min_calls,
        max_calls=max_calls,
        history=_turns(raw.get("history"), sid),
        expected_args=_validate_args(dict(raw.get("expected_args") or {}), sid),
    )


def load(path: Path | str = DEFAULT_PATH) -> list[Scenario]:
    """Read and validate the whole set. Duplicated ids are an error."""
    text = Path(path).read_text(encoding="utf-8")
    out: list[Scenario] = []
    seen: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            scenario = parse(line)
        except Exception as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
        if scenario.id in seen:
            raise ValueError(f"{path}:{lineno}: id {scenario.id!r} 가 중복된다")
        seen.add(scenario.id)
        out.append(scenario)
    if not out:
        raise ValueError(f"{path}: 시나리오가 하나도 없다")
    return out


def dataset_sha256(path: Path | str = DEFAULT_PATH) -> str:
    """Version of the set = hash of the file's bytes (eval/datasets 와 같은 규약)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- 자리표시자 ------------------------------------------------------------


class Resolver:
    """Turns ``{{doc:...}}`` / ``{{chunk:...}}`` into ids from a live index."""

    def __init__(self, documents: dict[str, str], chunks: dict[str, list[str]]):
        # filename -> document_id, filename -> [chunk_id...] (청크 순서대로)
        self._documents = documents
        self._chunks = chunks

    def value(self, kind: str, name: str, index: str | None) -> str:
        if kind == "doc":
            if name not in self._documents:
                raise KeyError(f"인덱스에 {name!r} 문서가 없다")
            return self._documents[name]
        ids = self._chunks.get(name)
        if not ids:
            raise KeyError(f"인덱스에 {name!r} 의 청크가 없다")
        i = int(index or 0)
        if i >= len(ids):
            raise KeyError(f"{name!r} 에는 청크가 {len(ids)}개뿐인데 {i} 을 요구했다")
        return ids[i]

    def text(self, s: str) -> str:
        return _PLACEHOLDER.sub(
            lambda m: self.value(m.group(1), m.group(2), m.group(3)), s
        )

    def structure(self, obj):
        """Substitute inside any nested list/dict of strings."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.structure(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.structure(v) for k, v in obj.items()}
        return obj

    def scenario(self, scenario: Scenario) -> Scenario:
        from dataclasses import replace

        return replace(
            scenario,
            utterance=self.text(scenario.utterance),
            history=tuple(
                Turn(role=t.role, text=self.text(t.text)) for t in scenario.history
            ),
            expected_args=self.structure(scenario.expected_args),
        )


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.search(text))
