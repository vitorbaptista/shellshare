'use strict';

var uuid = require('node-uuid');

module.exports = function(cookieName) {
  return function(req, res, next) {
    if (!req.cookies || req.cookies[cookieName]) {
      next();
      return;
    }

    res.cookie(cookieName, uuid.v4());
    next();
  };
};
