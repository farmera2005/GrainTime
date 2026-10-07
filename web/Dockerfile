# Serves the prebuilt web app (web/dist, committed to the repository) with
# nginx. Nothing is downloaded from npm when the stack is built, so the build
# works on networks that inspect or block package registries.
# After changing anything in web/src, run `npm run build` and commit web/dist.
FROM nginxinc/nginx-unprivileged:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY dist /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
