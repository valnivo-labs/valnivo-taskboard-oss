.PHONY: run demo test e2e docker
run:
	python3 server.py
demo:
	python3 server.py --demo
test:
	python3 selftest.py >/dev/null && python3 test_security.py
e2e:
	python3 e2e/test_ui.py
docker:
	docker compose up --build -d
