# Pinned point release so `docker compose build` on the Pi is reproducible.
# Bump deliberately — an Alpine roll can drop busybox applets the entrypoint
# scripts rely on.
FROM nginx:1.27.2-alpine

RUN apk add --no-cache git bash

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY init.sh /docker-entrypoint.d/40-clone-pokeapi.sh
RUN chmod +x /docker-entrypoint.d/40-clone-pokeapi.sh

EXPOSE 80
