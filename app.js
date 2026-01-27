'use strict';

var config = require('./config');

var express = require('express');
var http = require('http');
var path = require('path');
var { Server } = require('socket.io');
var logger = require('morgan');
var bodyParser = require('body-parser');
var errorHandler = require('errorhandler');

var db = require('./db');

var indexRoute = require('./routes/index');
var roomsRoute = require('./routes/rooms');

var app = express();
var server = http.createServer(app);

app.set('port', config.express.port);
app.set('views', path.join(__dirname, '/views'));
app.set('view engine', 'pug');
app.use(logger('dev'));
app.use(bodyParser.json({ limit: config.express.request_limit }));
app.use(express.static(path.join(__dirname, 'public'), {
  maxAge: 2678400000,  // one month
}));

if (config.env == 'development') {
  app.use(errorHandler());
  app.use(function(req, res, next) {
    res.locals.isDev = true;
    next();
  });
}

app.use('/', indexRoute);

db.connect(config.mongodb.uri, function(err) {
  if (err) {
    console.log('Unable to connect to Mongo.');
    process.exit(1);
  }

  server.listen(app.get('port'), config.express.host);
  var io = new Server(server);
  app.use('/r', roomsRoute('/r', io));

  console.log('Listening on http://' + (app.get('host') || 'localhost') + ':' + app.get('port'));
});
