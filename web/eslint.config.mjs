import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
  ]),
  {
    rules: {
      /* 이 앱은 브라우저 밖의 상태를 마운트 시점에 읽어야 한다 —
       * localStorage(테마·사이드바), matchMedia(좁은 화면 여부), 그리고
       * 세션·문서·대화·청크를 가져오는 네트워크 호출. 전부 서버 렌더에는
       * 없어서 초기값으로 옮길 수 없고, React가 효과를 쓰라고 지정한 바로
       * 그 경우다. 끄지는 않고 경고로 낮춘다 — 진짜로 파생 상태를 효과로
       * 계산하는 새 코드가 생기면 눈에 띄어야 하므로. */
      "react-hooks/set-state-in-effect": "warn",
    },
  },
]);

export default eslintConfig;
