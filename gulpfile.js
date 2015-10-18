'use strict';

var gulp = require('gulp');
var jshint = require('gulp-jshint');
var browserify = require('browserify');
var source = require('vinyl-source-stream');

gulp.task('lint', function() {
  gulp.src('./**/*.js')
      .pipe(jshint())
      .pipe(jshint.reporter('default'));
});

gulp.task('browserify', function() {
  return browserify('./public/javascript/room.js')
           .bundle()
           .pipe(source('room.bundle.js'))
           .pipe(gulp.dest('./public/javascript/'));
});
