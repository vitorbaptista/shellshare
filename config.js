var config = module.exports;

config.env = process.env.NODE_ENV || 'development';

config.express = {
  port: process.env.PORT || 3000,
}

config.mongodb = {
  uri: process.env.MONGOLAB_URI || 'mongodb://localhost:27017/shellshare',
  capped_size_limit: 50 * 1024,  // 50 KB is more than enough for a full terminal screen
}

config.cache = {
  ttl: 3600,
}
