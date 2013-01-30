
/**
 * Module dependencies.
 */

var express = require('express')
  , routes = require('./routes')
  , user = require('./routes/user')
  , http = require('http')
  , path = require('path')
  , io = require('socket.io');

var app = express()
  , server = http.createServer(app);

app.configure(function(){
  app.set('port', process.env.PORT || 3000);
  app.set('views', __dirname + '/views');
  app.set('view engine', 'jade');
  app.use(express.favicon());
  app.use(express.logger('dev'));
  app.use(express.bodyParser());
  app.use(express.methodOverride());
  app.use(app.router);
  app.use(require('less-middleware')({ src: __dirname + '/public' }));
  app.use(express.static(path.join(__dirname, 'public')));
});

app.configure('development', function(){
  app.use(express.errorHandler());
});

server.listen(app.get('port'));

app.get('/:room', user.join);
app.post('/:room', function (req, res) {
  var room = req.url,
      message = req.body.message;
  io.sockets.in(room).send(message);
  res.send('');
});
app.get('/', routes.index);

io = io.listen(server);

// Configuring for Heroku
io.configure(function () {
  io.set("transports", ["xhr-polling"]);
  io.set("polling duration", 10);
});

io.sockets.on('connection', function (socket) {
  socket.on('join', function (room) {
    socket.join(room);
    io.sockets.in(room).emit('usersCount', io.sockets.clients(room).length);
  });
});
