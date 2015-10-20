# shellshare

Live broadcast of terminal sessions.

## Why?

Ever wanted to quickly show what you're doing to some friends? Maybe you're seeing a weird error and would like some help. Or the other way around: some friend of yours is asking for help on something, then you start to ping-pong: you tell a command, he pastes the output, then you tell another, and so on...

The objective of [shellshare.net](http://shellshare.net) is to provide an easy way to broadcast your terminal live. No signups, no configurations, anything: simply run a command and you're good to go.

## Using

Copy and paste the following line in your terminal:

```
wget -qO shellshare http://get.shellshare.net; python shellshare
```

You should see a line saying `Sharing session in http://www.shellshare.net/r/h2Uont4F8bvZ8VDjHb` (your link will be different). Anyone that opens this link will be able to see what you're doing in your terminal. When you're done, type `exit` or hit CTRL+D.

## Installing

It requires Node 4.2.x (but should work with earlier versions), npm, Gulp and
MongoDB. Considering that these dependencies are installed on your local
machine, run:

```
npm install
gulp
```

This will run the server on [localhost:3000](http://localhost:3000). To
broadcast to this instance, use the `--server` option of the client, as
following:

```
./public/bin/shellshare --server localhost:3000
```

## Limitations

This project is intended for live broadcasts only. If you'd like to record your terminal, check [asciinema.org](https://asciinema.org).

# License

Copyright 2015 Vitor Baptista

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
