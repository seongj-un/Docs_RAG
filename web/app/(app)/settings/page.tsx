"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { AppShell } from "@/components/AppShell";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { UsageBar } from "@/components/ui/UsageBar";
import {
  describeError,
  usage as usageApi,
  type ErrorCopy,
  type Usage,
} from "@/lib/api";
import { useSession } from "@/lib/session";
import { useTheme, type Theme } from "@/lib/theme";
import styles from "./settings.module.css";

const THEMES: Theme[] = ["system", "light", "dark"];
const THEME_LABEL: Record<Theme, string> = {
  system: "기기 설정 따르기",
  light: "밝게",
  dark: "어둡게",
};

export default function SettingsPage() {
  const { theme, setTheme } = useTheme();
  const { user, signOut } = useSession();
  const router = useRouter();

  const [usage, setUsage] = useState<Usage | null>(null);
  const [usageError, setUsageError] = useState<ErrorCopy | null>(null);

  useEffect(() => {
    let cancelled = false;
    usageApi
      .getUsage()
      .then((value) => {
        if (!cancelled) setUsage(value);
      })
      .catch((cause) => {
        if (!cancelled) setUsageError(describeError(cause));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSignOut() {
    await signOut();
    router.replace("/login");
  }

  return (
    <AppShell crumb="설정">
      <div className={styles.screen}>
        <h1>설정</h1>

        <section className={styles.card}>
          <div className={styles.cardHead}>
            <h2>화면</h2>
            <p className={styles.cardNote}>
              기기 설정을 따르되, 원하면 직접 고를 수 있습니다.
            </p>
          </div>
          <div className={styles.segment}>
            {THEMES.map((option) => (
              <Button
                key={option}
                variant={theme === option ? "primary" : "default"}
                onClick={() => setTheme(option)}
                aria-pressed={theme === option}
              >
                {THEME_LABEL[option]}
              </Button>
            ))}
          </div>
        </section>

        <section className={styles.card}>
          <div className={styles.cardHead}>
            <h2>사용량</h2>
          </div>

          {usageError && (
            <Alert title={usageError.title}>{usageError.hint}</Alert>
          )}

          {!usageError && usage === null && (
            <p className={styles.cardNote}>불러오는 중…</p>
          )}

          {usage !== null && (
            <>
              <UsageBar
                label="오늘 한 질문"
                used={usage.queries_today}
                limit={usage.queries_per_day}
                unit="개"
                refill="내일 다시 채워집니다."
              />
              <UsageBar
                label="이번 달 올린 쪽수"
                used={usage.pages_this_month}
                limit={usage.pages_per_month}
                unit="쪽"
                refill="다음 달 1일에 다시 채워집니다."
              />
            </>
          )}
        </section>

        <section className={styles.card}>
          <div className={styles.cardHead}>
            <h2>계정</h2>
          </div>
          <div className={styles.row}>
            <span className={styles.value}>{user?.email}</span>
            <Button onClick={() => void onSignOut()}>로그아웃</Button>
          </div>
          {/* 계정 삭제는 아직 없다. 백엔드에 지우는 경로가 없어서, 눌러도
              아무 일도 없는 버튼을 두느니 두지 않는다. */}
        </section>
      </div>
    </AppShell>
  );
}
