(function () {
  'use strict';

  require('./vendor/base64');
  var io = require('socket.io-client');
  var Terminal = require('./vendor/term');
  var socket = io.connect('', {'sync disconnect on unload' : true});
  var room = window.location.pathname;
  var term;
  var currentSize = {};
  var onlineCounter = document.getElementById('online-counter');
  var onlineCounterPlural = document.getElementById('online-counter-plural');
  var terminalContainer = document.getElementById('terminal');

  Terminal.bindKeys = function() {
    // FIXME: Ugly monkey patch to avoid term.js capturing the key presses,
    // as we're just using term.js as read-only. There should be a better way
    // to do this, but I couldn't find it.
  };

  socket.emit('join', room);
  socket.on('usersCount', function (onlineUsers) {
    onlineCounter.innerHTML = onlineUsers;
    if (onlineUsers == 1) {
      onlineCounterPlural.innerHTML = '';
    } else {
      onlineCounterPlural.innerHTML = 's';
    }
  });
  socket.on('message', function (message) {
    if (term && message) {
      var decoded_message = decodeURIComponent(atob(message));
      term.write(decoded_message);
    }
  });
  socket.on('size', onSize);

  function onSize(size) {
    var sizeChanged;
    if (!size || !size.cols || !size.rows) {
      return;
    }

    sizeChanged = (currentSize.cols != size.cols ||
                   currentSize.rows != size.rows);

    if (term && sizeChanged) {
      term.destroy();
    }

    if (!term || sizeChanged) {
      term = new Terminal({
        cols: size.cols,
        rows: size.rows,
        body: terminalContainer,
      });
      term.open();
    }

    currentSize = size;
  }

  // Init terminal with a default size.
  onSize({cols: 80, rows: 30});
})();
