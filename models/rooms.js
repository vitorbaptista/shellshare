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
    });
  }
}

function push(room, size, message, callback) {
  _collection(room, function(collection) {
    collection.insertOne({message: message, size: size}, callback);
  });
}

function all(room, callback) {
  _collection(room, function(collection) {
    collection.find({}).toArray(function(err, items) {
      if (err || items.length == 0) {
        callback(err, items);
      } else {
        var messageAscii = items.map(function (item) {
            return new Buffer(item.message, 'base64').toString('ascii');
        }).join('');
        var item = {
          size: items[items.length - 1].size,
          message: new Buffer(messageAscii).toString('base64'),
        };

        callback(err, item);
      }
    });
  });
}

function drop(room, callback) {
  _collection(room, function(collection) {
    cache.del(room);
    collection.drop(callback);
  });
}

module.exports = {
  push: push,
  all: all,
  drop: drop,
}
