(function () {
  var socket = io.connect('', {'sync disconnect on unload' : true}),
      room = window.location.pathname,
      term = new Term(80, 30);

  socket.emit('join', room);
  socket.on('usersCount', function (onlineUsers) {
    document.getElementById('online-counter').innerHTML = onlineUsers;
  });
  socket.on('message', function (message) {
    term.write(atob(message));
  });

  term.open();

  window.onresize = function () {
    var termContainer = document.getElementById('main'),
        termFontWidth = 8;
    term.w = termContainer.offsetWidth / termFontWidth;
  };
  window.onresize();
})();
