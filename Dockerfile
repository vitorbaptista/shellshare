FROM nodesource/node:4.2.5

RUN apt-get update
RUN apt-get install -y gnupg libkrb5-dev
RUN curl https://www.mongodb.org/static/pgp/server-4.0.asc | apt-key add -
RUN echo "deb http://repo.mongodb.org/apt/debian jessie/mongodb-org/4.0 main" | tee /etc/apt/sources.list.d/mongodb-org-4.0.list

RUN apt-get update
RUN apt-get install -y mongodb-org
RUN npm install -g gulp-cli


COPY . /shellshare/

WORKDIR /shellshare
RUN mkdir -p /data/db
RUN npm set unsafe-perm true
RUN npm install
CMD ["/shellshare/run_services"]
