'use strict';

var express = require('express');
var analytics = require('../analytics');
var router = express.Router();
var roomPrefix;
var io;
var authorizationModel;
var roomsModel;

/* GET room. */
router.get('/:room', function(req, res, next) {
  res.render('room');
  next();
}, analytics.pageview());

/* POST room. */
router.post('/:room', function(req, res, next) {
  authorizeOrDie(req, res, function() {
    var room = req.url;
    var size = JSON.parse(req.body.size);
    var message = req.body.message;
    var analyticsEventOptions = {};

    io.sockets.in(room).emit('size', size);
    io.sockets.in(room).emit('message', message);

    roomsModel.push(room, size, message);

    if (size.rows && size.cols) {
      // Use screenResolution as the terminal's size.
      analyticsEventOptions.sr = size.rows + 'x' + size.cols;
    }
    analytics.sendEvent(req, 'rooms', 'write', room, message.length,
                        analyticsEventOptions);

    res.sendStatus(200);
  });
});

/* DELETE room */
router.delete('/:room', function(req, res, next) {
  authorizeOrDie(req, res, function() {
    var room = req.url;
    roomsModel.drop(room);
    analytics.sendEvent(req, 'rooms', 'delete', room);
    res.sendStatus(202);
  });
});

function authorizeOrDie(req, res, callback) {
  var room = req.url;
  var secret = req.get('Authorization');

  // FIXME: secret might be empty
  authorizationModel.isAuthorized(room, secret, function(authorized) {
    if (!authorized) {
      analytics.sendEvent(req, 'rooms', 'authorization', 'failure');
      res.sendStatus(401);
    } else {
      analytics.sendEvent(req, 'rooms', 'authorization', 'success');
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
};
