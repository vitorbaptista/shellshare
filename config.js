'use strict';

var config = module.exports;

config.env = process.env.NODE_ENV || 'development';

config.express = {
  host: process.env.HOST,
  port: process.env.PORT || 3000,
  request_limit: '300kb',
};

config.mongodb = {
  uri: process.env.MONGODB_URI || process.env.MONGO_URL || 'mongodb://localhost:27017/shellshare',
  capped_size_limit: 400 * 1024,  // 400 KB
  capped_size_max: 30,
  authorizations_ttl: 86400,  // 1 day
};

config.cache = {
  ttl: 3600,
};
