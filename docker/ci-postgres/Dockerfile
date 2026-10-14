# CI-образ PostgreSQL для тестов: postgres:16-alpine, настроенный под одноразовую базу.
# Собирать в среде с доступом к Docker Hub, затем пушить в Nexus:
#   docker build --platform linux/amd64 -t registry.genai.int/ci/postgres:16-alpine .
#   docker push registry.genai.int/ci/postgres:16-alpine
FROM postgres:16-alpine

CMD ["postgres", "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
