(function () {
  var socket = io.connect('', {'sync disconnect on unload' : true}),
      room = window.location.pathname,
      term;

  socket.emit('join', room);
  socket.on('usersCount', function (onlineUsers) {
    document.getElementById('online-counter').innerHTML = onlineUsers;
  });
  socket.on('message', function (message) {
    if (term) {
      term.write(atob(message));
    }
  });
  socket.on('size', function (size) {
    if (!term) {
      size = JSON.parse(size)
      term = new Terminal(size.cols, size.rows);
      term.open();
    }
  });
})();
