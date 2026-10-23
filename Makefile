.PHONY: run demo test docker
run:
	python3 server.py
demo:
	python3 server.py --demo
test:
	python3 selftest.py >/dev/null && python3 test_security.py
docker:
	docker compose up --build -d
