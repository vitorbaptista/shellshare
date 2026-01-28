FROM node:22

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN npm install -g gulp-cli

COPY . /shellshare/

WORKDIR /shellshare
RUN npm install

EXPOSE 3000
CMD ["npm", "start"]
