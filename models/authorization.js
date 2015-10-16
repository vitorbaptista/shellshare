var db = require('../db');
var config = require('../config');

var _collection = db.get().collection('authorizations');

function isAuthorized(room, secret, callback) {
  _collection.findOne({"_id": room}, function(err, item) {
    if (!item) {
      _collection.insertOne({"_id": room, "secret": secret});
      callback(true);
    } else {
      callback(item.secret == secret);
    }
  });
}

module.exports = {
  isAuthorized: isAuthorized,
}
