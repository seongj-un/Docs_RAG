"use client";

import { useRef, useState, type DragEvent } from "react";

import { Alert } from "@/components/ui/Alert";
import { describeError, type ErrorCopy } from "@/lib/api";
import { useDocuments } from "@/lib/documents";
import styles from "./UploadDropzone.module.css";

type UploadDropzoneProps = {
  /** 빈 채팅 화면에서는 문구가 다르다. */
  title?: string;
  hint?: string;
};

export function UploadDropzone({
  title = "여기에 PDF를 끌어다 놓으세요",
  hint = "누르면 파일을 고를 수도 있습니다. 한 번에 하나씩, 50MB·500쪽까지.",
}: UploadDropzoneProps) {
  const { upload } = useDocuments();
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorCopy | null>(null);

  async function accept(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;

    setError(null);
    setBusy(true);
    try {
      await upload(file);
    } catch (cause) {
      setError(describeError(cause));
    } finally {
      setBusy(false);
      /* 같은 파일을 다시 고를 수 있게 비운다 — 값이 남아 있으면 change가
       * 안 일어난다. */
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function onDrop(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setOver(false);
    void accept(event.dataTransfer.files);
  }

  return (
    <div className="flex w-full flex-col gap-3">
      <button
        type="button"
        className={styles.zone}
        data-over={over}
        disabled={busy}
        onClick={() => inputRef.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <span className={styles.title}>
          {busy ? "올리는 중…" : title}
        </span>
        <span className={styles.hint}>{hint}</span>
      </button>

      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        hidden
        onChange={(event) => void accept(event.target.files)}
      />

      {error && (
        <Alert title={error.title} urgent>
          {error.hint}
        </Alert>
      )}
    </div>
  );
}
