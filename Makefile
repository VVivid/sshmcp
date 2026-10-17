PYTHON ?= python3

.PHONY: lab-up lab-down lab-status lab-test lab-reset

lab-up:
	$(PYTHON) tools/lab.py up

lab-down:
	$(PYTHON) tools/lab.py down

lab-status:
	$(PYTHON) tools/lab.py status

lab-test:
	$(PYTHON) tools/lab.py test

lab-reset:
	$(PYTHON) tools/lab.py reset

.PHONY: target-up target-down target-status target-test target-reset target-runner-install runner-check

target-up:
	$(PYTHON) tools/lab.py up --profile target

target-down:
	$(PYTHON) tools/lab.py down --profile target

target-status:
	$(PYTHON) tools/lab.py status --profile target

target-test:
	$(PYTHON) tools/lab.py test --profile target

target-reset:
	$(PYTHON) tools/lab.py reset --profile target

target-runner-install:
	$(PYTHON) tools/lab.py runner-install --profile target

runner-check:
	go test -race ./...
	go vet ./...
