(function () {
  var socket = io.connect(),
      room = window.location.pathname,
      term = new Term(80, 30);

  socket.emit('join', room);
  socket.on('usersCount', function (onlineUsers) {
    document.getElementById('online-counter').innerHTML = onlineUsers;
  });

  term.open();

  window.onresize = function () {
    var termContainer = document.getElementById('main'),
        termFontWidth = 8;
    term.w = termContainer.offsetWidth / termFontWidth;
  };
  window.onresize();
})();
