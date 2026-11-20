IMAGE ?= blast-radius-entry
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl
VERIFY_DIR := $(CURDIR)/.verification

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check synthetic

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Generate and label deterministic mutations with the full suite, then score the selector.
synthetic: build
	mkdir -p "$(VERIFY_DIR)"
	chmod a+w "$(VERIFY_DIR)"
	docker run $(RUN_FLAGS) \
		--volume "$(VERIFY_DIR):/verification" \
		--volume "$(CURDIR)/verify_synthetic.py:/verify_synthetic.py:ro" \
		$(IMAGE) python /verify_synthetic.py
	$(MAKE) sample SAMPLE="$(VERIFY_DIR)/synthetic" OUTPUT="$(VERIFY_DIR)/synthetic.jsonl"
