FROM node:14

RUN apt-get update
RUN apt-get install -y gnupg libkrb5-dev
RUN npm install -g gulp-cli


COPY . /shellshare/

WORKDIR /shellshare
RUN npm set unsafe-perm true
RUN npm install
# HEALTHCHECK --interval=5m30s --timeout=3s CMD curl -f http://localhost:3000/ || exit 1

EXPOSE 3000
CMD npm start
