'use strict';

var gulp = require('gulp');
var rename = require('gulp-rename');

function lint(cb) {
  var jshint = require('gulp-jshint');

  return gulp.src(['./*.js', './routes/*.js', './models/*.js', './public/javascript/*.js', '!./public/javascript/*.bundle.js', '!./public/javascript/*.min.js'])
      .pipe(jshint())
      .pipe(jshint.reporter('default'));
}
gulp.task(lint);

function browserify(cb) {
  var browserify = require('browserify');
  var source = require('vinyl-source-stream');

  return browserify('./public/javascript/room.js')
           .bundle()
           .pipe(source('room.bundle.js'))
           .pipe(gulp.dest('./public/javascript/'));
}
gulp.task(browserify);

function minifyCss() {
  var cleanCss = require('gulp-clean-css');

  return gulp.src(['!./**/*.min.css', 'public/stylesheet/**/*.css'])
             .pipe(cleanCss({compatibility: 'ie8'}))
             .pipe(rename({suffix: '.min'}))
             .pipe(gulp.dest('public/stylesheet'));
}
gulp.task(minifyCss);

function _minifyJs() {
  var uglify = require('gulp-uglify');

  return gulp.src(['!./**/*.min.js', './public/javascript/*.js'])
             .pipe(uglify())
             .pipe(rename({suffix: '.min'}))
             .pipe(gulp.dest('./public/javascript/'));
}
gulp.task('minifyJs', gulp.series(browserify, _minifyJs));

gulp.task('build:production', gulp.parallel(minifyCss, gulp.task('minifyJs')));

gulp.task('default', gulp.task('build:production'));
