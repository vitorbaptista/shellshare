(function () {
  var socket = io.connect('', {'sync disconnect on unload' : true}),
      room = window.location.pathname,
      term = new Terminal(80, 38);

  socket.emit('join', room);
  socket.on('usersCount', function (onlineUsers) {
    document.getElementById('online-counter').innerHTML = onlineUsers;
  });
  socket.on('message', function (message) {
    term.write(atob(message));
  });

  term.open();
})();
