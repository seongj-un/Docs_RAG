# Next.js 프론트엔드.
#
# NEXT_PUBLIC_* 은 런타임이 아니라 **빌드 시점에 번들에 박힌다.** 그래서
# 이미지에 호스트 주소를 굽지 않으려면 API 주소가 상대 경로여야 하고,
# 그 전제를 프록시가 만들어 준다 — Caddy 가 같은 오리진에서 / 는 이 서비스로,
# /api/* 는 백엔드로 보낸다. 덕분에 이미지 하나가 localhost 든 도메인이든
# 그대로 돌고, 브라우저 입장에서 오리진이 하나라 CORS 자체가 없다.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:24-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_BASE=/api
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
# 타입 검사가 여기서 함께 돈다 — vitest 는 타입을 보지 않으므로,
# 빌드가 타입 오류를 잡는 유일한 관문이다.
RUN npm run build

FROM node:24-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -g 10003 -S nodejs && adduser -u 10003 -G nodejs -S nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
