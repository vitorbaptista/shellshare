(function () {
  var term = new Term(80, 30);

  term.open();

  window.onresize = function () {
    var termContainer = document.getElementById('main'),
        termFontWidth = 8;
    term.w = termContainer.offsetWidth / termFontWidth;
  };
  window.onresize();
})();
