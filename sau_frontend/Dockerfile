FROM node:22.21.1-alpine AS builder

WORKDIR /app

RUN npm config set registry https://registry.npmmirror.com

COPY package*.json ./
RUN npm install

COPY . .

# 替换 API 地址为空，使用相对路径通过 OpenResty 访问后端
RUN sed -i 's#http://localhost:5409##g' /app/.env.production
RUN sed -i "s#import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'#''#g" /app/src/api/material.js
RUN sed -i "s#import.meta.env.VITE_API_BASE_URL || 'http://localhost:5409'#''#g" /app/src/utils/request.js

RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
