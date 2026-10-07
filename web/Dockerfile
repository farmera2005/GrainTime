# syntax=docker/dockerfile:1
# Debian-based Node (glibc), matching the platform the lockfile is generated on;
# Vite's bundler ships platform-specific native binaries.
FROM node:22-bookworm-slim AS build
WORKDIR /src
# Retry flaky downloads instead of failing (npm can otherwise die with
# "Exit handler never called!" on a dropped connection). The cache mount keeps
# already-downloaded packages between attempts.
ENV npm_config_fetch_retries=5 \
    npm_config_fetch_retry_mintimeout=20000 \
    npm_config_fetch_retry_maxtimeout=120000 \
    npm_config_fetch_timeout=600000 \
    npm_config_update_notifier=false
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund --loglevel=http
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
