SHELL := /bin/bash
.PHONY: build benchmark deploy

build:
	cargo build --release

ROOM ?= testroom
PASS ?= testpass
SERVER ?= http://localhost:3000
REQUESTS ?= 1000
CONCURRENCY ?= 10

benchmark:
	ab -n $(REQUESTS) -c $(CONCURRENCY) \
		-T 'application/json' \
		-H 'Authorization: $(PASS)' \
		-p <(echo '{"message": "SGVsbG8=", "size": {"cols": 80, "rows": 24}}') \
		$(SERVER)/r/$(ROOM)

deploy:
	git push dokku master:master
