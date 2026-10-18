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

.PHONY: mcp-lab-up mcp-lab-down mcp-lab-status mcp-lab-login mcp-lab-test

mcp-lab-up:
	$(PYTHON) tools/mcp_lab.py up

mcp-lab-down:
	$(PYTHON) tools/mcp_lab.py down

mcp-lab-status:
	$(PYTHON) tools/mcp_lab.py status

mcp-lab-login:
	$(PYTHON) tools/mcp_lab.py login

mcp-lab-test:
	$(PYTHON) tests/integration_mcp.py -v

.PHONY: agent-demo-test agent-demo-up agent-demo-down agent-demo-reset agent-demo-operations-test

agent-demo-test:
	$(PYTHON) tools/agent_demo.py test

agent-demo-operations-test:
	$(PYTHON) tools/demo_operations.py

agent-demo-up:
	$(PYTHON) tools/agent_demo.py up

agent-demo-down:
	$(PYTHON) tools/agent_demo.py down

agent-demo-reset:
	$(PYTHON) tools/agent_demo.py reset
