# syntax=docker/dockerfile:1
# AgentOS UI image: static Vite build served by non-root nginx.
#
# BUILD FROM THE REPO ROOT, not from apps/ui/, because the UI resolves the frozen
# ACI TypeScript types through a path alias (@aci -> ../../packages/aci-protocol/
# generated/ts, in both tsconfig and vite), which lives outside apps/ui. The repo
# layout is replicated in the build stage so that relative alias resolves:
#
#   docker build -f apps/ui/Dockerfile -t agentos-ui .
#
# apps/ui/Dockerfile.dockerignore (BuildKit honors <dockerfile>.dockerignore over
# the context-root one) trims the root context to the UI plus that one
# cross-package tree.
FROM node:22-slim AS build
WORKDIR /app
# Pin pnpm explicitly: package.json has no packageManager field, so corepack
# would otherwise pull whatever pnpm is latest. pnpm 10+ turns a dependency with
# an unapproved build script (esbuild, which vite needs) into a hard install
# failure; pnpm 9 runs those scripts by default and reads this lockfile
# (lockfileVersion 9.0) unchanged, so the build is reproducible without an
# approve-builds step or a package.json edit.
RUN corepack enable && corepack prepare pnpm@9.15.9 --activate
# Install deps first (own cache layer) from the UI's self-contained lockfile.
COPY apps/ui/package.json apps/ui/pnpm-lock.yaml ./apps/ui/
WORKDIR /app/apps/ui
RUN pnpm install --frozen-lockfile
# The frozen ACI types the @aci alias points at, then the UI sources. Both land
# at their repo-relative paths so `../../packages/...` resolves from apps/ui.
WORKDIR /app
COPY packages/aci-protocol/generated ./packages/aci-protocol/generated
COPY apps/ui ./apps/ui
WORKDIR /app/apps/ui
RUN pnpm build

FROM nginxinc/nginx-unprivileged:1.27-alpine
# Only AGENTOS_API_TARGET is substituted; nginx's own $vars are left intact.
ENV AGENTOS_API_TARGET=http://agentos-api:8000 \
    NGINX_ENVSUBST_FILTER=AGENTOS_API_TARGET
COPY --from=build /app/apps/ui/dist /usr/share/nginx/html
COPY <<'EOF' /etc/nginx/templates/default.conf.template
server {
    listen 8080;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    # Reverse-proxy API calls, stripping the /api prefix.
    location /api/ {
        proxy_pass ${AGENTOS_API_TARGET}/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # SPA fallback.
    location / {
        try_files $uri $uri/ /index.html;
    }
}
EOF
EXPOSE 8080
