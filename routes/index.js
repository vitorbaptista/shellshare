'use strict';

var express = require('express');
var analytics = require('../analytics');
var router = express.Router();

/* GET home page. */
router.get('/', function(req, res, next) {
  res.locals.isLinux = _isLinux(req.get('user-agent'));

  res.render('index');
  next();
}, analytics.pageview());

function _isLinux(userAgent) {
  return (userAgent || '').toLowerCase().search('linux') !== -1;
}

module.exports = router;
