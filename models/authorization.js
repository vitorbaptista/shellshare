var db = require('../db');
var config = require('../config');

var _collection_cache = null;

function _collection() {
  if (_collection_cache == null) {
    _collection_cache = db.get().collection('authorizations');
    _collection_cache.ensureIndex('updatedAt', { expireAfterSeconds: 86400 });
  }
  return _collection_cache;
}

function isAuthorized(room, secret, callback) {
  _collection().findOne({'_id': room}, function(err, item) {
    if (!item || item.secret == secret) {
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
      callback(true);
    } else {
      callback(false);
    }
  });
}

module.exports = {
  isAuthorized: isAuthorized,
}
