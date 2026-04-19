FROM nginx:alpine

RUN apk add --no-cache git bash

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY init.sh /docker-entrypoint.d/40-clone-pokeapi.sh
RUN chmod +x /docker-entrypoint.d/40-clone-pokeapi.sh

EXPOSE 80
