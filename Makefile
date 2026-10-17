PYTHON ?= python3

.PHONY: lab-up lab-down lab-status

lab-up:
	$(PYTHON) tools/lab.py up

lab-down:
	$(PYTHON) tools/lab.py down

lab-status:
	$(PYTHON) tools/lab.py status
