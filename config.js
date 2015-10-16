var config = module.exports;

config.env = process.env.NODE_ENV || 'development';

config.express = {
  port: process.env.PORT || 3000,
}

config.mongodb = {
  uri: process.env.MONGOLAB_URI || 'mongodb://localhost:27017/shellshare',
}
