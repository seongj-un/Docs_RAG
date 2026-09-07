"use client";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";

/* 색을 쓰지 않는 디자인에서 '위험한 버튼'은 형태로 표현되지 않는다.
 * 노션 M5의 미해결 항목이었고, 확인 한 단계를 넣는 쪽으로 정했다 —
 * 되돌릴 수 없다는 사실은 빨간색보다 마찰과 문구가 더 정확히 전한다. */
type ConfirmDialogProps = {
  open: boolean;
  title: string;
  /** 무엇이 사라지는지. 되돌릴 수 없다면 그렇게 적는다. */
  consequence: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
};

export function ConfirmDialog({
  open,
  title,
  consequence,
  confirmLabel,
  onConfirm,
  onCancel,
  busy = false,
}: ConfirmDialogProps) {
  return (
    <Modal open={open} title={title} onClose={onCancel}>
      <div className="flex flex-col gap-4">
        <Alert title="되돌릴 수 없습니다">{consequence}</Alert>
        <div className="flex justify-end gap-2">
          <Button onClick={onCancel} disabled={busy}>
            그만두기
          </Button>
          <Button variant="primary" onClick={onConfirm} disabled={busy}>
            {busy ? "지우는 중…" : confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
