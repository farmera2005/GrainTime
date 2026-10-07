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
# Extra root certificates from ./certs (TLS-inspecting networks; see
# certs/README.md). Node only uses them when there are some.
COPY --from=certs . /tmp/extra-certs/
RUN mkdir -p /etc/ssl/extra; \
    for f in /tmp/extra-certs/*.crt /tmp/extra-certs/*.pem; do \
      if [ -f "$f" ]; then cat "$f"; echo; fi; \
    done > /etc/ssl/extra/ca.pem; \
    rm -rf /tmp/extra-certs
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    if [ -s /etc/ssl/extra/ca.pem ]; then export NODE_EXTRA_CA_CERTS=/etc/ssl/extra/ca.pem; fi; \
    npm ci --no-audit --no-fund --loglevel=http
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
