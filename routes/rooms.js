var express = require('express');
var router = express.Router();
var roomPrefix;
var io;
var authorizationModel;
var roomsModel;

/* GET room. */
router.get('/:room', function(req, res, next) {
  res.render('join');
  next();
});

/* POST room. */
router.post('/:room', function(req, res, next) {
  var room = req.url;
  var secret = req.get('Authorization');

  authorizeOrDie(room, secret, function() {
    var size = req.body.size;
    var message = req.body.message;

    io.sockets.in(room).emit('size', size);
    io.sockets.in(room).emit('message', message);

    roomsModel.push(room, size, message);
    res.sendStatus(200);
  });
});

/* DELETE room */
router.delete('/:room', function(req, res, next) {
  var room = req.url;
  var secret = req.get('Authorization');

  authorizeOrDie(room, secret, function() {
    roomsModel.drop(room, function(err, col) {
    });
    res.sendStatus(202);
  });
});

function authorizeOrDie(room, secret, callback) {
  // FIXME: secret might be empty
  authorizationModel.isAuthorized(room, secret, function(authorized) {
    if (!authorized) {
      res.sendStatus(401);
    } else {
      callback();
    }
  });
}

function setupSockets() {
  io.sockets.on('connection', function (socket) {
    var rooms = [];

    socket.on('join', function (room) {
      room = stripPrefix(room);
      socket.join(room, function (err) {
        if (!err) {
          rooms.push(room);
          updateUsersCount(io, room);
          roomsModel.all(room, function(err, data) {
            if (!err) {
              socket.emit('size', data.size);
              socket.emit('message', data.message);
            }
          });
        }
      });
    });

    socket.on('disconnect', function () {
      for (var i in rooms) {
        updateUsersCount(io, rooms[i]);
      }
    });
  });
}

function stripPrefix(room) {
  if (roomPrefix === room.slice(0, roomPrefix.length)) {
    return room.slice(roomPrefix.length);
  } else {
    return room;
  }
}

function updateUsersCount(io, room) {
  var clients = io.sockets.adapter.rooms[room];

  if (clients !== undefined) {
    io.sockets.in(room).emit('usersCount', Object.keys(clients).length);
  }
}

module.exports = function(_roomPrefix, _io) {
  roomPrefix = _roomPrefix;
  io = _io;
  authorizationModel = require('../models/authorization');
  roomsModel = require('../models/rooms');

  setupSockets();
  return router;
}
