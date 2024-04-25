build_base:

	cd services/api && make build_base
	cd services/request && make build_base
	cd services/model && make build_base


build_service:

	cd services/api && make build_service
	cd services/request && make build_service
	cd services/model && make build_service

up:
	docker compose up --detach

down:
	docker compose down

	docker container prune --force
	docker volume prune --force

up-dev:
	docker compose -f docker-compose-dev.yml up --detach

down-dev:
	docker compose -f docker-compose-dev.yml down

	docker container prune --force
	docker volume prune --force