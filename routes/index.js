'use strict';

var express = require('express');
var analytics = require('../analytics');
var router = express.Router();

/* GET home page. */
router.get('/', function(req, res, next) {
  res.render('index');
  next();
}, analytics.pageview());

module.exports = router;
