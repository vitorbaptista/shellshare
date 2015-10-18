'use strict';

var gulp = require('gulp');
var jshint = require('gulp-jshint');

gulp.task('lint', function() {
  gulp.src('./**/*.js')
      .pipe(jshint())
      .pipe(jshint.reporter('default'));
});
