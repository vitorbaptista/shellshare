var config = require('./config');
var NodeCache = require('node-cache');

exports.createStore = function() {
  return new NodeCache({
    stdTTL: config.cache.ttl,
    useClones: false,
  });
}
