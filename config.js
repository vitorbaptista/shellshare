'use strict';

var config = module.exports;

config.env = process.env.NODE_ENV || 'development';

config.express = {
  host: process.env.HOST,
  port: process.env.PORT || 3000,
  request_limit: '300kb',
};

config.mongodb = {
  uri: process.env.MONGOLAB_URI || 'mongodb://localhost:27017/shellshare',
  capped_size_limit: 400 * 1024,  // 400 KB
  capped_size_max: 30,
  authorizations_ttl: 86400,  // 1 day
};

config.cache = {
  ttl: 3600,
};

config.analytics = {
  tracking_id: process.env.GOOGLE_ANALYTICS_TID,
};

config.newrelic = {
  app_name: 'shellshare',
  license_key: process.env.NEWRELIC_KEY,
  log_level: 'info',
};
