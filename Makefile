SHELL := /bin/bash
.PHONY: build lint benchmark deploy release

build:
	cargo build --release

lint:
	cargo clippy
	cargo check

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

# make release            -> bump patch (2.0.6 -> 2.0.7), tag, push; CI does the rest
# make release VERSION=x.y.z
release:
	scripts/release.sh $(VERSION)
