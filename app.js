'use strict';

var express = require('express');
var http = require('http');
var path = require('path');
var io = require('socket.io');
var logger = require('morgan');
var bodyParser = require('body-parser');
var errorHandler = require('errorhandler');
var cookieParser = require('cookie-parser');

var config = require('./config');
var db = require('./db');
var analytics = require('./analytics');
var configureSession = require('./middleware/session');

var indexRoute = require('./routes/index');
var roomsRoute = require('./routes/rooms');

var app = express();
var server = http.createServer(app);

app.set('port', config.express.port);
app.set('views', path.join(__dirname, '/views'));
app.set('view engine', 'jade');
app.use(logger('dev'));
app.use(bodyParser.json());
app.use(bodyParser.urlencoded({ extended: true }));
app.use(express.static(path.join(__dirname, 'public')));
app.use(cookieParser());

if (config.env == 'development') {
  app.use(errorHandler());
  app.use(function(req, res, next) {
    res.locals.isDev = true;
    next();
  });
}// else {
  app.use(configureSession('_ga'));
  app.use(analytics.middleware(analytics.trackingId));
//}

app.use('/', indexRoute);

db.connect(config.mongodb.uri, function(err) {
  if (err) {
    console.log('Unable to connect to Mongo.');
    process.exit(1);
  }

  server.listen(app.get('port'));
  io = io.listen(server);
  app.use('/r', roomsRoute('/r', io));
});
