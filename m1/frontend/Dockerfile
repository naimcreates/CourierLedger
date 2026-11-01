# CourierLedger M1 -- frontend image (React + TypeScript + Vite).
# Local development only. Build from m1/: `docker compose build frontend`
# (context is ./frontend). Builds the production bundle and serves it
# with `vite preview` on 5173. NOTE: the Vite dev proxy (/api ->
# 127.0.0.1:8000) is a dev-server feature and is NOT wired for
# `vite preview`; the containerised frontend therefore does not proxy
# API calls. The supported local flow stays the Vite dev server
# (`npm run dev`), and the compose frontend service stays commented out
# until the preview proxy is configured for the compose network. This
# Dockerfile exists so the build itself is verified, not to claim a
# working containerised frontend.
FROM node:24-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

EXPOSE 5173

CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "5173"]
