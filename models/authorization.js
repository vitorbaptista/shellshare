'use strict';

var db = require('../db');
var config = require('../config');
var cache = require('../cache').createStore();

function _collection() {
  var collection = cache.get('authorizations-collection');
  if (!collection) {
    collection = db.get().collection('authorizations');
    collection.ensureIndex('updatedAt', {
      expireAfterSeconds: config.mongodb.authorizations_ttl
    });
    cache.set('authorizations-collection', collection);
  }
  return collection;
}

function isAuthorized(room, secret, callback) {
  var cache_key = 'room:' + room;
  var authorization = cache.get(cache_key);
  if (authorization) {
    if (authorization == secret) {
      _upsertAuthorization(room, secret);
      callback(true);
    } else {
      callback(false);
    }
  } else {
    _collection().findOne({'_id': room}, function(err, item) {
      if (item) {
        cache.set(cache_key, item.secret);
      }

      if (!item || item.secret == secret) {
        _upsertAuthorization(room, secret);
        callback(true);
      } else {
        callback(false);
      }
    });
  }
}

function _upsertAuthorization(room, secret) {
  _collection().updateOne({
    '_id': room,
  },
  {
    '_id': room,
    'secret': secret,
    'updatedAt': new Date(),
  },
  {
    upsert: true,
  });
}

module.exports = {
  isAuthorized: isAuthorized,
};
