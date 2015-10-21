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

  if (window.navigator && window.navigator.platform) {
    var platform = window.navigator.platform.toLowerCase();
    if (platform.search('linux') != -1) {
      linuxElement.checked = true;
    }
  }

  macosElement.onclick = toggleOS;
  linuxElement.onclick = toggleOS;

  toggleOS();
})();
