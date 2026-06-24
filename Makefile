PYTHON ?= python3

.PHONY: test integration-test check install uninstall

test:
	$(PYTHON) -m py_compile src/chaotic_review.py tests/test_chaotic_review.py
	$(PYTHON) -m unittest discover -s tests -v

integration-test:
	./tests/integration_pacman.sh

check: test integration-test

install:
	./scripts/install.sh

uninstall:
	./scripts/uninstall.sh
