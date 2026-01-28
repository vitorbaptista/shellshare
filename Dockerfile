FROM node:22-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN npm install -g gulp-cli

WORKDIR /shellshare

# Copy package files and install deps (skip postinstall since gulpfile isn't there yet)
COPY package*.json ./
RUN npm install --ignore-scripts

# Copy source files and build assets
COPY . .
RUN gulp build:production

EXPOSE 3000
CMD ["npm", "start"]
