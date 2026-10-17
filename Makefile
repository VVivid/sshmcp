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
