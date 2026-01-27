'use strict';

// Analytics stub - Google Analytics removed
// These functions are kept for backward compatibility but do nothing

function pageview(options) {
  return function(req, res, next) {
    next();
  };
}

function sendEvent(req, category, action, label, value, options) {
  // no-op
}

module.exports = {
  trackingId: null,
  middleware: function() { return function(req, res, next) { next(); }; },
  pageview: pageview,
  sendEvent: sendEvent,
};
