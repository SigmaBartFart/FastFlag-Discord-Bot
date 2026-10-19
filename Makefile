.PHONY: test docker-build docker-run

test:
	python -m unittest discover --start-directory tests --pattern "test_*.py" --verbose

docker-build:
	docker build --tag roblox-fflag-bot:local .

docker-run:
	docker run --rm --env-file .env --volume "$(CURDIR)/data:/app/data" roblox-fflag-bot:local
