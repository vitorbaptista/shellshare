
/**
 * Module dependencies.
 */

var express = require('express')
  , routes = require('./routes')
  , user = require('./routes/user')
  , http = require('http')
  , path = require('path')
  , io = require('socket.io')
  , memjs = require('memjs');

var app = express()
  , server = http.createServer(app)
  , memcached = memjs.Client.create();

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
      size = req.body.size,
      message = req.body.message;
  memcached.get(room, function (error, secret) {
    var authorization = req.get('Authorization'),
        authorized = !secret || secret.toString() == authorization;
    if (!authorized || !authorization) {
      res.send(401);
      return;
    }
    if (!secret) {
      memcached.set(room, authorization);
    }
    io.sockets.in(room).emit('size', size);
    io.sockets.in(room).emit('message', message);
    res.send(200);
  });
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

  socket.on('disconnect', function () {
    var rooms = io.sockets.manager.roomClients[socket.id];
    for (var room in rooms) {
      if (room !== '') {
        var usersCount = io.sockets.manager.rooms[room].length - 1,
            realRoom = room.substr(1); // We need to remove the starting
                                       // '/' from the room name
        io.sockets.in(realRoom).emit('usersCount', usersCount);
      }
    }
  });
});
