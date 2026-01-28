FROM node:22-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN npm install -g gulp-cli

WORKDIR /shellshare
COPY package*.json ./
RUN npm install

COPY . .
RUN gulp build:production

EXPOSE 3000
CMD ["npm", "start"]
