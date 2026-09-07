import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* 컨테이너 배포용. .next/standalone 에 필요한 node_modules 만 추린
   * 서버를 내놓아, 런타임 이미지에 개발 의존성을 통째로 넣지 않아도 된다. */
  output: "standalone",
};

export default nextConfig;
