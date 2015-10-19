'use strict';

var ua = require('universal-analytics');
var url = require('url');
var extend = require('extend');
var config = require('./config');

function pageview(options) {
  return function(req, res, next) {
    var defaults = {
      dp: req.path,
      dh: _getUrl(req),
      dr: req.get('Referer'),
      uip: req.ip,
      ua: req.get('User-Agent'),
    };

    if (req.visitor) {
      req.visitor
         .pageview(extend(defaults, options))
         .send();
    }

    next();
  };
}

function sendEvent(req, category, action, label, value, options) {
  var defaults = {
    t: 'event',
    ni: true,
    ec: category,
    ea: action,
    el: label,
    ev: value,
  };

  pageview(extend(defaults, options))(req, {}, function(){});
}

function _getUrl(req) {
  return url.format({
    protocol: req.protocol,
    host: req.get('host'),
  });
}

module.exports = {
  trackingId: config.analytics.tracking_id,
  middleware: ua.middleware,
  pageview: pageview,
  sendEvent: sendEvent,
};
