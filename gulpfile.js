'use strict';

var gulp = require('gulp');
var jshint = require('gulp-jshint');
var browserify = require('browserify');
var source = require('vinyl-source-stream');
var minifyCss = require('gulp-minify-css');
var rename = require('gulp-rename');
var uglify = require('gulp-uglify');

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

gulp.task('minify-css', function() {
  return gulp.src('public/stylesheet/*.css')
             .pipe(minifyCss({compatibility: 'ie8'}))
             .pipe(rename({suffix: '.min'}))
             .pipe(gulp.dest('public/stylesheet'));
});

gulp.task('minify-js', ['browserify'], function() {
  return gulp.src('./public/javascript/room.bundle.js')
             .pipe(uglify())
             .pipe(rename({suffix: '.min'}))
             .pipe(gulp.dest('./public/javascript/'));
});
