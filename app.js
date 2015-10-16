
/**
 * Module dependencies.
 */

var express = require('express')
  , routes = require('./routes')
  , user = require('./routes/user')
  , http = require('http')
  , path = require('path')
  , io = require('socket.io')
  , memjs = require('memjs')
  , logger = require('morgan')
  , bodyParser = require('body-parser')
  , errorHandler = require('errorhandler');

var app = express()
  , server = http.createServer(app)
  , memcached = memjs.Client.create();

app.set('port', process.env.PORT || 3000);
app.set('views', path.join(__dirname, '/views'));
app.set('view engine', 'jade');
app.use(logger('dev'));
app.use(bodyParser.json());
app.use(bodyParser.urlencoded({ extended: true }));
app.use(express.static(path.join(__dirname, 'public')));

if ('development' == app.get('env')) {
  app.use(errorHandler());
}

server.listen(app.get('port'));

app.route('/:room')
   .get(user.join)
   .post(function (req, res) {
     var room = req.url,
         size = req.body.size,
         message = req.body.message;
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
       io.sockets.in(room).emit('size', size);
       io.sockets.in(room).emit('message', message);
       res.sendStatus(200);
     });
   });
app.get('/', routes.index);

io = io.listen(server);

io.sockets.on('connection', function (socket) {
  var rooms = [];

  socket.on('join', function (room) {
    socket.join(room, function (err) {
      if (!err) {
        rooms.push(room);
        updateUsersCount(io, room);
      }
    });
  });

  socket.on('disconnect', function () {
    for (var i in rooms) {
      updateUsersCount(io, rooms[i]);
    }
  });
});

function updateUsersCount(io, room) {
  var clients = io.sockets.adapter.rooms[room];

  if (clients !== undefined) {
    io.sockets.in(room).emit('usersCount', Object.keys(clients).length);
  }
}
