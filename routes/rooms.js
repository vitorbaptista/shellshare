var express = require('express');
var router = express.Router();

/* GET room. */
router.get('/:room', function(req, res, next) {
  res.render('join');
  next();
});

/* POST room. */
router.post('/:room', function(req, res, next) {
  var room = req.url;
  // FIXME: secret might be empty
  var secret = req.get('Authorization');
  var authorizationModel = require('../models/authorization');

  authorizationModel.isAuthorized(room, secret, function (authorized) {
    if (!authorized) {
      res.sendStatus(401);
      return;
    }
    next();
  });
});

module.exports = router;
