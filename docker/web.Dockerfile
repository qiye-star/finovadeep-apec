# 静态站镜像：Nginx 提供 finova-website，并把 /apec-api/ 转给 chat 服务
# docker build -f docker/web.Dockerfile -t apec-web .
FROM nginx:1.27-alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY finova-website/ /usr/share/nginx/html/

EXPOSE 80
