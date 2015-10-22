'use strict';

var config = require('./config');

if (config.newrelic.license_key) {
  require('newrelic');
}

var express = require('express');
var http = require('http');
var path = require('path');
var io = require('socket.io');
var logger = require('morgan');
var bodyParser = require('body-parser');
var errorHandler = require('errorhandler');

var db = require('./db');
var analytics = require('./analytics');


var indexRoute = require('./routes/index');
var roomsRoute = require('./routes/rooms');

var app = express();
var server = http.createServer(app);

app.set('port', config.express.port);
app.set('views', path.join(__dirname, '/views'));
app.set('view engine', 'jade');
app.use(logger('dev'));
app.use(bodyParser.json({ limit: config.express.request_limit }));
app.use(express.static(path.join(__dirname, 'public')));

if (config.env == 'development') {
  app.use(errorHandler());
  app.use(function(req, res, next) {
    res.locals.isDev = true;
    next();
  });
} else {
  if (analytics.trackingId) {
    app.use(analytics.middleware(analytics.trackingId));
  }
}

app.use('/', indexRoute);

db.connect(config.mongodb.uri, function(err) {
  if (err) {
    console.log('Unable to connect to Mongo.');
    process.exit(1);
  }

  server.listen(app.get('port'), config.express.host);
  io = io.listen(server);
  app.use('/r', roomsRoute('/r', io));
});
