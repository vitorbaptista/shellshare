'use strict';

var db = require('../db');
var config = require('../config');
var cache = require('../cache').createStore();

function _collection(room, callback) {
  var collection = cache.get(room);

  if (collection) {
    callback(collection);
  } else {
    var collectionName = 'rooms:' + room;

    db.get().createCollection(collectionName, {
      capped: true,
      size: config.mongodb.capped_size_limit,
    }).then(function(collection) {
      cache.set(room, collection);
      callback(collection);
    }).catch(function(err) {
      // Collection might already exist, just get it
      var collection = db.get().collection(collectionName);
      cache.set(room, collection);
      callback(collection);
    });
  }
}

function push(room, size, message, callback) {
  _collection(room, function(collection) {
    collection.insertOne({message: message, size: size})
      .then(function() {
        if (callback) callback(null);
      })
      .catch(function(err) {
        console.error('Push error:', err);
        if (callback) callback(err);
      });
  });
}

function all(room, callback) {
  _collection(room, function(collection) {
    collection.find({}).toArray()
      .then(function(items) {
        if (items.length === 0) {
          callback(null, null);
        } else {
          var messageAscii = items.map(function (item) {
              return Buffer.from(item.message, 'base64').toString('ascii');
          }).join('');
          var item = {
            size: items[items.length - 1].size,
            message: Buffer.from(messageAscii).toString('base64'),
          };
          callback(null, item);
        }
      })
      .catch(function(err) {
        console.error('All error:', err);
        callback(err, null);
      });
  });
}

function drop(room, callback) {
  _collection(room, function(collection) {
    cache.del(room);
    collection.drop()
      .then(function() {
        if (callback) callback(null);
      })
      .catch(function(err) {
        console.error('Drop error:', err);
        if (callback) callback(err);
      });
  });
}

module.exports = {
  push: push,
  all: all,
  drop: drop,
};
