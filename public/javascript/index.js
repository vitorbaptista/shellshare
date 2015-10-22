(function () {
  'use strict';

  var macosElement = document.getElementById('os-macos');
  var linuxElement = document.getElementById('os-linux');

  function toggleOS() {
    if (linuxElement.checked) {
      document.body.className = 'instructions-linux';
    } else {
      document.body.className = 'instructions-macos';
    }
  }

  macosElement.onclick = toggleOS;
  linuxElement.onclick = toggleOS;

  toggleOS();
})();
