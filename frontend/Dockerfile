# Stage 1: build the React app.  Stage 2: Nginx serves it AND load-balances /api to node-a/b/c.
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/tatkalx_common.inc /etc/nginx/tatkalx_common.inc
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
