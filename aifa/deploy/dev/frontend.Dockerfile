# Образ фронтенда AIFA (ADR-0015: экран прежний). Контекст — aifa/web. По образцу Dockerfile.frontend
# AIFA-CLAW: те же стадии, пути без префикса `web/`.
#   docker build -f aifa/deploy/dev/frontend.Dockerfile --build-arg NEXT_PUBLIC_BUILD_ID=<sha> \
#       -t aifa-sym-frontend:dev aifa/web

FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

FROM node:20-alpine AS builder
WORKDIR /app
# Публичные переменные Next.js запекаются при сборке. Отпечаток сборки печётся и в клиентский
# бандл, и в ручку /build-id: по нему вкладка узнаёт, что её код устарел.
ARG NEXT_PUBLIC_API_BASE_URL=/api
ARG NEXT_PUBLIC_BUILD_ID=dev
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_BUILD_ID=$NEXT_PUBLIC_BUILD_ID \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production
COPY --from=deps /app/node_modules ./node_modules
COPY . ./
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
