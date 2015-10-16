var express = require('express');
var router = express.Router();
var memjs = require('memjs');
var memcached = memjs.Client.create();

/* GET room. */
router.get('/:room', function(req, res, next) {
  res.render('join');
});

/* POST room. */
router.post('/:room', function(req, res, next) {
  var room = req.url;

  memcached.get(room, function (error, secret) {
    var authorization = req.get('Authorization'),
        authorized = !secret || secret.toString() == authorization;
    if (!authorized || !authorization) {
      res.sendStatus(401);
      return;
    }
    if (!secret) {
      memcached.set(room, authorization);
    }

    next();
  });
});

module.exports = router;
