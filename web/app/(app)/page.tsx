"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { UploadDropzone } from "@/components/UploadDropzone";
import { AnswerText } from "@/components/chat/AnswerText";
import { CitationModal } from "@/components/chat/CitationModal";
import { Composer, type Scope } from "@/components/chat/Composer";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import {
  ApiError,
  conversations as api,
  describeError,
  type Citation,
  type ErrorCopy,
  type Message,
} from "@/lib/api";
import { useConversations } from "@/lib/conversations";
import { useDocuments } from "@/lib/documents";
import { annotate, type Footnote } from "@/lib/footnotes";
import styles from "./chat.module.css";
import thread from "@/components/chat/Thread.module.css";

/** 서버가 준 것과 낙관적으로 끼워 넣은 것을 같은 모양으로 다룬다.
 *
 * scope_document_id는 이 턴을 물었을 때의 범위이고 서버가 기록한다. 거부
 * 안내가 "지금 보고 있는 문서 안에는" 같은 말을 하려면 그때의 범위를 알아야
 * 하는데, 화면의 현재 범위를 읽으면 셀렉터를 바꾸는 순간 지난 답변의
 * 설명까지 바뀐다. 실시간 턴도 같은 필드에 담아, 방금 받은 답변과 다시
 * 열어본 이력이 같은 규칙으로 읽히게 한다. */
type LocalMessage = Message & { pending?: boolean };

function localMessage(
  role: "user" | "assistant",
  content: string,
  scope: Scope = null,
): LocalMessage {
  return {
    id: `local-${crypto.randomUUID()}`,
    role,
    content,
    citations: null,
    refused: false,
    scope_document_id: scope,
    created_at: new Date().toISOString(),
    pending: true,
  };
}

export default function ChatPage() {
  /* useSearchParams는 Suspense 경계를 요구한다 (Next 16). */
  return (
    <Suspense fallback={null}>
      <Chat />
    </Suspense>
  );
}

function Chat() {
  const searchParams = useSearchParams();
  const { documents } = useDocuments();
  const { create, refresh: refreshConversations } = useConversations();

  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  /** 스트리밍 중 쌓이는 글자. null이면 진행 중인 턴이 없다. */
  const [streaming, setStreaming] = useState<string | null>(null);
  const [turnError, setTurnError] = useState<ErrorCopy | null>(null);
  /** 실패한 질문. 재시도 버튼이 이걸 다시 보낸다. */
  const [lastQuestion, setLastQuestion] = useState<string | null>(null);
  /* 문서 상세의 "이 문서에 질문하기"가 범위를 들고 온다. 효과로 맞추지 않고
   * 초기값으로 읽는다 — 다른 경로에서 넘어오는 것이라 이 화면은 새로
   * 마운트되고, 효과로 하면 첫 프레임이 잘못된 범위로 한 번 그려진다. */
  const [scope, setScope] = useState<Scope>(
    () => searchParams.get("document") || null,
  );
  const [openFootnote, setOpenFootnote] = useState<{
    footnotes: Footnote[];
    number: number;
  } | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  /* 지금 화면에 올라온 대화의 id. 주소와 비교하는 기준이며, 상태가 아니라
   * ref다 — 첫 질문에서 대화를 만든 직후 주소가 아직 안 바뀐 찰나에
   * 상태로 비교하면 방금 만든 대화를 새 대화로 오인해 지운다. */
  const loadedId = useRef<string | null>(null);
  const router = useRouter();

  /* 새 글자가 붙을 때마다 바닥을 따라간다. */
  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, streaming]);

  const openConversation = useCallback(async (id: string) => {
    setConversationId(id);
    setMessages([]);
    setStreaming(null);
    setTurnError(null);
    try {
      const detail = await api.getConversation(id);
      /* 응답이 기대한 모양이 아니어도 화면이 죽지는 않게 한다. */
      setMessages(detail.messages ?? []);
      setScope(detail.scope_document_id ?? null);
    } catch (cause) {
      /* 404(없는 대화)와 422(id가 UUID 형식이 아님)는 사용자에게 같은
       * 상황이다 — 이 주소로는 열 수 없다. 공용 문구는 각각 "목록을 새로
       * 불러와 주세요"와 "잠시 뒤에 다시 시도해 주세요"라고 하는데, 여기선
       * 목록이 멀쩡하고 다시 시도해도 결과가 같다. 안내한 해결 방법과
       * 실제로 주는 행동(새 대화 시작하기)이 어긋나지 않게 따로 말한다. */
      if (
        cause instanceof ApiError &&
        (cause.status === 404 || cause.status === 422)
      ) {
        setTurnError({
          title: "이 대화를 찾을 수 없습니다",
          hint: "이미 지워졌거나, 주소가 잘못됐을 수 있습니다.",
        });
      } else {
        setTurnError(describeError(cause));
      }
    }
  }, []);

  function resetThread() {
    setConversationId(null);
    setMessages([]);
    setStreaming(null);
    setTurnError(null);
    setLastQuestion(null);
  }

  /* 빈 문자열은 "고르지 않음"이다. 그대로 두면 /conversations/ 를 부르게
   * 되고, 그 경로는 목록 라우트로 넘어가 배열이 돌아온다. */
  const conversationParam = searchParams.get("c") || null;
  useEffect(() => {
    if (conversationParam === loadedId.current) return;
    loadedId.current = conversationParam;
    if (conversationParam === null) {
      // 주소(외부 상태)에 화면을 맞추는 동기화다. 파생 상태 계산이 아니다.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      resetThread();
    } else {
      void openConversation(conversationParam);
    }
    // resetThread는 상태 setter만 부르므로 의존성에 넣지 않는다.
  }, [conversationParam, openConversation]);


  const ask = useCallback(
    async (question: string) => {
      setTurnError(null);
      setLastQuestion(question);
      setMessages((current) => [...current, localMessage("user", question)]);
      setStreaming("");

      let id = conversationId;
      let settled = false;

      try {
        if (id === null) {
          /* 첫 질문에서야 대화를 만든다. 열어놓고 아무것도 묻지 않은
           * 빈 대화가 사이드바에 쌓이지 않게. */
          id = (await create(scope)).id;
          setConversationId(id);
          /* 주소를 먼저 맞춰야 위 효과가 이 대화를 새 대화로 오인하지 않는다. */
          loadedId.current = id;
          router.replace(`/?c=${id}`);
        }

        for await (const event of api.streamQuery(id, question, {
          documentId: scope,
        })) {
          if (event.type === "token") {
            setStreaming((current) => (current ?? "") + event.text);
          } else if (event.type === "done") {
            settled = true;
            setMessages((current) => [
              ...current,
              {
                id: `local-${crypto.randomUUID()}`,
                role: "assistant",
                content: event.answer,
                citations: event.citations,
                refused: event.refused,
                scope_document_id: scope,
                created_at: new Date().toISOString(),
              },
            ]);
            setStreaming(null);
          } else if (event.type === "error") {
            settled = true;
            setStreaming(null);
            /* 스트림이 열린 뒤의 실패는 상태 코드가 아니라 사건으로 온다.
             * 문구 고르는 규칙은 같으므로 같은 ApiError로 되돌린다. */
            setTurnError(describeError(new ApiError(event.status, event.detail)));
          }
        }

        if (!settled) {
          /* done도 error도 없이 끝났다 — 연결이 끊긴 것이다. */
          setStreaming(null);
          setTurnError({
            title: "답변이 도중에 끊겼습니다",
            hint: "다시 물어봐 주세요.",
          });
        }
      } catch (cause) {
        setStreaming(null);
        setTurnError(describeError(cause));
      } finally {
        /* 첫 질문이 제목을 만든다. 사이드바에 반영하려면 다시 읽어야 한다. */
        void refreshConversations();
      }
    },
    [conversationId, create, scope, refreshConversations, router],
  );

  function openFootnoteFor(footnotes: Footnote[], number: number) {
    setOpenFootnote({ footnotes, number });
  }

  const hasReadyDocument = documents.some(
    (document) => document.status === "ready",
  );
  const busy = streaming !== null;

  return (
    <AppShell crumb="대화">
      <div className={styles.screen}>
        <div className={styles.scroll} ref={scrollRef}>
          {/* 에러가 있으면 빈 상태를 내지 않는다 — "무엇을 찾아드릴까요"와
              "찾을 수 없습니다"가 같이 떠 있으면 무슨 일이 났는지 알 수 없다. */}
          {messages.length === 0 && streaming === null && turnError === null && (
            <div className={styles.empty}>
              <div className={styles.emptyLead}>
                <h1>무엇을 찾아드릴까요</h1>
                <p className={styles.emptyHint}>
                  {hasReadyDocument
                    ? "올려둔 문서 안에서 근거를 찾아 답합니다."
                    : "먼저 문서를 올리면 그 안에서 답을 찾습니다."}
                </p>
              </div>
              {!hasReadyDocument && <UploadDropzone />}
            </div>
          )}

          {/* 에러도 여기 들어간다. 예전엔 messages.length > 0 안에만 있어서,
              대화를 못 불러온 경우(메시지가 비어 있는 그 경우)에 정작 아무
              말도 하지 않았다. */}
          {(messages.length > 0 || streaming !== null || turnError !== null) && (
            <div className={thread.thread}>
              {messages.map((message) => (
                <Turn
                  key={message.id}
                  message={message}
                  onWiden={() => setScope(null)}
                  onSelectFootnote={openFootnoteFor}
                />
              ))}

              {streaming !== null && (
                <div className={thread.turn}>
                  <AnswerText
                    annotated={annotate(streaming, [])}
                    streaming
                    onSelectFootnote={() => undefined}
                  />
                </div>
              )}

              {turnError && (
                <Alert
                  title={turnError.title}
                  urgent
                  actions={
                    /* 질문하다 실패했으면 그 질문을 다시 보내면 된다.
                       대화를 여는 데 실패한 것이라면 다시 시도해도 같은
                       결과이므로, 나갈 길을 준다. */
                    lastQuestion ? (
                      <Button onClick={() => void ask(lastQuestion)}>
                        다시 시도
                      </Button>
                    ) : (
                      <Button onClick={() => router.push("/")}>
                        새 대화 시작하기
                      </Button>
                    )
                  }
                >
                  {turnError.hint}
                </Alert>
              )}
            </div>
          )}
        </div>

        <Composer
          scope={scope}
          onScopeChange={setScope}
          onSubmit={(question) => void ask(question)}
          disabled={busy}
        />
      </div>

      {openFootnote && (
        <CitationModal
          footnotes={openFootnote.footnotes}
          activeNumber={openFootnote.number}
          onSelect={(number) =>
            setOpenFootnote((current) =>
              current ? { ...current, number } : null,
            )
          }
          onClose={() => setOpenFootnote(null)}
        />
      )}
    </AppShell>
  );
}

type TurnProps = {
  message: LocalMessage;
  onWiden: () => void;
  onSelectFootnote: (footnotes: Footnote[], number: number) => void;
};

function Turn({ message, onWiden, onSelectFootnote }: TurnProps) {
  if (message.role === "user") {
    return <p className={thread.question}>{message.content}</p>;
  }

  /* 거부는 답변이 아니라 알림으로 낸다. 카피 규칙대로 막다른 길이 아니라
   * 다음 행동을 준다 — 한 문서만 보고 물었다면 전체로 넓혀볼 수 있다.
   *
   * 기준은 이 턴을 물었을 때의 범위(서버가 기록한 scope_document_id)다.
   * 화면의 현재 범위를 읽으면 셀렉터를 건드리는 순간 지난 거부의 설명이
   * 사실과 달라진다. */
  if (message.refused) {
    const asked = message.scope_document_id;

    return (
      <Alert
        title="문서에서 찾을 수 없습니다"
        actions={
          asked !== null ? (
            <Button onClick={onWiden}>전체 문서에서 다시 찾기</Button>
          ) : undefined
        }
      >
        {asked !== null
          ? "지금 보고 있는 문서 안에는 이 질문에 답할 내용이 없습니다."
          : "올려둔 문서 어디에도 이 질문에 답할 내용이 없습니다. 다르게 물어보거나 문서를 더 올려 주세요."}
      </Alert>
    );
  }

  const citations: Citation[] = message.citations ?? [];
  const annotated = annotate(message.content, citations);
  const all = [...annotated.footnotes, ...annotated.orphans];

  return (
    <div className={thread.turn}>
      <AnswerText
        annotated={annotated}
        onSelectFootnote={(number) => onSelectFootnote(all, number)}
      />
    </div>
  );
}
