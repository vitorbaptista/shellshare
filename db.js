'use strict';

var { MongoClient } = require('mongodb');

var state = {
  db: null,
  client: null,
};

exports.connect = function(url, done) {
  if (state.db) return done();

  MongoClient.connect(url)
    .then(function(client) {
      state.client = client;
      state.db = client.db();
      done();
    })
    .catch(function(err) {
      done(err);
    });
};

exports.get = function() {
  return state.db;
};

exports.close = function(done) {
  if (state.client) {
    state.client.close()
      .then(function() {
        state.db = null;
        state.client = null;
        done();
      })
      .catch(done);
  } else {
    done();
  }
};
