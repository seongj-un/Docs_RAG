"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { StatusMark } from "@/components/ui/StatusMark";
import { Steps, type Step } from "@/components/ui/Steps";
import { describeError, type Document, type ErrorCopy } from "@/lib/api";
import { useDocuments } from "@/lib/documents";
import { FAILURE_NOTE, FAILURE_TITLE, failureKind } from "@/lib/failure";
import styles from "../documents.module.css";

/* 백엔드가 실제로 구분하는 것은 pending/processing/ready/failed 넷뿐이다.
 * 색인 중에는 num_pages도 아직 커밋되지 않아서 "글자 읽기"와 "정리하기"를
 * 나눌 근거가 없다 — 없는 진행률을 지어내느니 아는 만큼만 보여준다. */
function stepsFor(document: Document): Step[] {
  const failed = document.status === "failed";
  const ready = document.status === "ready";
  const kind = failureKind(document.error);

  return [
    { label: "문서 올리기", state: "done" },
    {
      label: failed ? FAILURE_TITLE[kind] : "글자 읽고 정리하기",
      state: failed ? "failed" : ready ? "done" : "active",
      note: failed
        ? FAILURE_NOTE[kind]
        : ready
          ? `${document.num_pages ?? 0}쪽을 읽었습니다.`
          : "조금만 기다려 주세요. 문서가 길수록 오래 걸립니다.",
    },
    {
      label: "질문 가능",
      state: ready ? "done" : "upcoming",
    },
  ];
}

export default function DocumentDetailPage({
  params,
}: {
  /* Next 16부터 params는 Promise다. 클라이언트 컴포넌트에서는 use()로 푼다. */
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const { byId, loading, remove } = useDocuments();
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<ErrorCopy | null>(null);

  const document = byId(id);

  async function confirmDelete() {
    setDeleting(true);
    setDeleteError(null);
    try {
      await remove(id);
      router.replace("/documents");
    } catch (cause) {
      setDeleteError(describeError(cause));
      setDeleting(false);
    }
  }

  if (loading) {
    return (
      <AppShell crumb="문서">
        <div className={styles.screen}>
          <p className={styles.subtitle}>불러오는 중…</p>
        </div>
      </AppShell>
    );
  }

  if (!document) {
    return (
      <AppShell crumb="문서">
        <div className={styles.screen}>
          <Alert
            title="이 문서를 찾을 수 없습니다"
            actions={
              <Link href="/documents">
                <Button>문서 목록으로</Button>
              </Link>
            }
          >
            이미 지워졌을 수 있습니다.
          </Alert>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell crumb="문서">
      <div className={styles.screen}>
        <div className={styles.head}>
          <h1>{document.filename}</h1>
          <div className={styles.meta}>
            <StatusMark status={document.status} error={document.error} />
            {document.num_pages !== null && <span>{document.num_pages}쪽</span>}
          </div>
        </div>

        {deleteError && (
          <Alert title={deleteError.title} urgent>
            {deleteError.hint}
          </Alert>
        )}

        <Steps steps={stepsFor(document)} />

        {document.status === "ready" && (
          <Alert
            title="이 문서에 질문할 수 있습니다"
            actions={
              <Link href={`/?document=${document.id}`}>
                <Button variant="primary">이 문서에 질문하기</Button>
              </Link>
            }
          >
            대화 화면에서 질의 범위를 이 문서로 두고 물어봅니다.
          </Alert>
        )}

        <div className="flex justify-end">
          <Button onClick={() => setConfirming(true)}>이 문서 지우기</Button>
        </div>
      </div>

      <ConfirmDialog
        open={confirming}
        title={`${document.filename} 지우기`}
        consequence="이 문서와 그 안에서 찾은 내용이 모두 사라집니다. 이 문서를 근거로 삼았던 지난 답변은 남지만, 근거를 다시 열어볼 수는 없습니다."
        confirmLabel="지우기"
        busy={deleting}
        onConfirm={() => void confirmDelete()}
        onCancel={() => {
          setConfirming(false);
          setDeleteError(null);
        }}
      />
    </AppShell>
  );
}
