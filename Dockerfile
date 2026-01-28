FROM node:22

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN npm install -g gulp-cli

WORKDIR /shellshare
COPY . .

RUN npm install --ignore-scripts
RUN npm run postinstall

EXPOSE 3000
CMD ["npm", "start"]
