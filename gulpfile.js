'use strict';

var gulp = require('gulp');
var rename = require('gulp-rename');

function lint(cb) {
  var jshint = require('gulp-jshint');

  return gulp.src('./**/*.js')
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
  var minifyCss = require('gulp-minify-css');

  return gulp.src(['!./**/*.min.css', 'public/stylesheet/**/*.css'])
             .pipe(minifyCss({compatibility: 'ie8'}))
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

function _start() {
  var nodemon = require('gulp-nodemon');
    nodemon({
      script: 'app.js',
      ext: 'js',
      tasks: ['lint', 'browserify'],
      ignore: ['*.bundle.js', '*.min.js'],
      env: {'NODE_ENV': 'development'}
    });
}
gulp.task('start', gulp.series(lint, browserify, _start));

gulp.task('build:development', gulp.series(lint, browserify));
gulp.task('build:production', gulp.parallel(minifyCss, gulp.task('minifyJs')));

gulp.task('default', gulp.task('start'));
