'use strict';

var gulp = require('gulp');
var rename = require('gulp-rename');

gulp.task('lint', function() {
  var jshint = require('gulp-jshint');

  gulp.src('./**/*.js')
      .pipe(jshint())
      .pipe(jshint.reporter('default'));
});

gulp.task('browserify', function() {
  var browserify = require('browserify');
  var source = require('vinyl-source-stream');

  return browserify('./public/javascript/room.js')
           .bundle()
           .pipe(source('room.bundle.js'))
           .pipe(gulp.dest('./public/javascript/'));
});

gulp.task('minify-css', function() {
  var minifyCss = require('gulp-minify-css');

  return gulp.src('public/stylesheet/*.css')
             .pipe(minifyCss({compatibility: 'ie8'}))
             .pipe(rename({suffix: '.min'}))
             .pipe(gulp.dest('public/stylesheet'));
});

gulp.task('minify-js', ['browserify'], function() {
  var uglify = require('gulp-uglify');

  return gulp.src('./public/javascript/room.bundle.js')
             .pipe(uglify())
             .pipe(rename({suffix: '.min'}))
             .pipe(gulp.dest('./public/javascript/'));
});

gulp.task('start', ['lint', 'browserify'], function() {
  var nodemon = require('gulp-nodemon');

  nodemon({
    script: 'app.js',
    ext: 'js',
    tasks: ['lint', 'browserify'],
    ignore: ['*.bundle.js', '*.min.js'],
    env: {'NODE_ENV': 'development'}
  });
});

gulp.task('build:development', ['lint', 'browserify']);
gulp.task('build:production', ['minify-css', 'minify-js']);

gulp.task('default', ['start']);
