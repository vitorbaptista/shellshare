'use strict';

var db = require('../db');
var config = require('../config');
var cache = require('../cache').createStore();

var indexCreated = false;

function _collection() {
  var collection = db.get().collection('authorizations');
  
  // Create TTL index if not already done
  if (!indexCreated) {
    collection.createIndex(
      { updatedAt: 1 },
      { expireAfterSeconds: config.mongodb.authorizations_ttl }
    ).catch(function(err) {
      // Index might already exist, ignore error
      console.log('Index creation note:', err.message);
    });
    indexCreated = true;
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
    _collection().findOne({'_id': room})
      .then(function(item) {
        if (item) {
          cache.set(cache_key, item.secret);
        }

        if (!item || item.secret == secret) {
          _upsertAuthorization(room, secret);
          callback(true);
        } else {
          callback(false);
        }
      })
      .catch(function(err) {
        console.error('Authorization error:', err);
        callback(false);
      });
  }
}

function _upsertAuthorization(room, secret) {
  _collection().updateOne(
    { '_id': room },
    {
      $set: {
        '_id': room,
        'secret': secret,
        'updatedAt': new Date(),
      }
    },
    { upsert: true }
  ).catch(function(err) {
    console.error('Upsert authorization error:', err);
  });
}

module.exports = {
  isAuthorized: isAuthorized,
};
