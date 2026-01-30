(function () {
  'use strict';

  var macosElement = document.getElementById('os-macos');
  var linuxElement = document.getElementById('os-linux');
  var windowsElement = document.getElementById('os-windows');

  function toggleOS() {
    if (linuxElement.checked) {
      document.body.className = 'instructions-linux';
    } else if (windowsElement.checked) {
      document.body.className = 'instructions-windows';
    } else {
      document.body.className = 'instructions-macos';
    }
  }

  macosElement.addEventListener('change', toggleOS);
  linuxElement.addEventListener('change', toggleOS);
  windowsElement.addEventListener('change', toggleOS);

  // Auto-detect OS
  var platform = navigator.platform || '';
  var userAgent = navigator.userAgent || '';

  if (platform.indexOf('Win') !== -1 || userAgent.indexOf('Windows') !== -1) {
    windowsElement.checked = true;
  } else if (platform.indexOf('Linux') !== -1) {
    linuxElement.checked = true;
  }
  // macOS is already checked by default in HTML

  toggleOS();
})();
