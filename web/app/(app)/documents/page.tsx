"use client";

import Link from "next/link";
import { useState } from "react";

import { AppShell } from "@/components/AppShell";
import { UploadDropzone } from "@/components/UploadDropzone";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { StatusMark } from "@/components/ui/StatusMark";
import { describeError, type Document, type ErrorCopy } from "@/lib/api";
import { useDocuments } from "@/lib/documents";
import styles from "./documents.module.css";

export default function DocumentsPage() {
  const { documents, loading, error, remove } = useDocuments();
  const [pendingDelete, setPendingDelete] = useState<Document | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<ErrorCopy | null>(null);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await remove(pendingDelete.id);
      setPendingDelete(null);
    } catch (cause) {
      setDeleteError(describeError(cause));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <AppShell crumb="문서">
      <div className={styles.screen}>
        <div className={styles.head}>
          <h1>문서</h1>
          <p className={styles.subtitle}>
            올린 문서가 여기 모입니다. 다 읽은 문서에만 질문할 수 있습니다.
          </p>
        </div>

        <UploadDropzone />

        {error && <Alert title={error.title}>{error.hint}</Alert>}
        {deleteError && (
          <Alert title={deleteError.title} urgent>
            {deleteError.hint}
          </Alert>
        )}

        {loading && <p className={styles.subtitle}>불러오는 중…</p>}

        {!loading && documents.length === 0 && (
          <Alert title="아직 올린 문서가 없습니다">
            위 상자에 PDF를 끌어다 놓으면 시작됩니다.
          </Alert>
        )}

        <div className={styles.list}>
          {documents.map((document) => (
            <article key={document.id} className={styles.card}>
              <div className={styles.cardBody}>
                <Link
                  href={`/documents/${document.id}`}
                  className={styles.name}
                >
                  {document.filename}
                </Link>
                <div className={styles.meta}>
                  <StatusMark
                    status={document.status}
                    error={document.error}
                  />
                  {document.num_pages !== null && (
                    <span>{document.num_pages}쪽</span>
                  )}
                </div>
              </div>
              <Button onClick={() => setPendingDelete(document)}>지우기</Button>
            </article>
          ))}
        </div>
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`${pendingDelete?.filename ?? ""} 지우기`}
        consequence="이 문서와 그 안에서 찾은 내용이 모두 사라집니다. 이 문서를 근거로 삼았던 지난 답변은 남지만, 근거를 다시 열어볼 수는 없습니다."
        confirmLabel="지우기"
        busy={deleting}
        onConfirm={() => void confirmDelete()}
        onCancel={() => {
          setPendingDelete(null);
          setDeleteError(null);
        }}
      />
    </AppShell>
  );
}
