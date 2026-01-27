'use strict';

var express = require('express');
var router = express.Router();
var roomPrefix;
var io;
var authorizationModel;
var roomsModel;

/* GET room. */
router.get('/:room', function(req, res, next) {
  res.render('room');
});

/* POST room. */
router.post('/:room', function(req, res, next) {
  authorizeOrDie(req, res, function() {
    var room = req.url;
    var size = req.body.size;
    var message = req.body.message;

    io.to(room).emit('size', size);
    io.to(room).emit('message', message);

    roomsModel.push(room, size, message);

    res.sendStatus(200);
  });
});

/* DELETE room */
router.delete('/:room', function(req, res, next) {
  authorizeOrDie(req, res, function() {
    var room = req.url;
    roomsModel.drop(room);
    res.sendStatus(202);
  });
});

function authorizeOrDie(req, res, callback) {
  var room = req.url;
  var secret = req.get('Authorization');

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
  io.on('connection', function (socket) {
    var rooms = [];

    socket.on('join', function (room) {
      room = stripPrefix(room);
      socket.join(room);
      rooms.push(room);
      updateUsersCount(io, room);
      roomsModel.all(room, function(err, data) {
        if (!err && data) {
          socket.emit('size', data.size);
          socket.emit('message', data.message);
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
  var clients = io.sockets.adapter.rooms.get(room);

  if (clients !== undefined) {
    io.to(room).emit('usersCount', clients.size);
  }
}

module.exports = function(_roomPrefix, _io) {
  roomPrefix = _roomPrefix;
  io = _io;
  authorizationModel = require('../models/authorization');
  roomsModel = require('../models/rooms');

  setupSockets();
  return router;
};
