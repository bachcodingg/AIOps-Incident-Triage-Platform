# syntax=docker/dockerfile:1
FROM maven:3.9-eclipse-temurin-21 AS deps
WORKDIR /src
COPY pom.xml ./
RUN --mount=type=cache,target=/root/.m2 mvn -B -ntp -q dependency:go-offline

# `docker build --target test` runs the unit tests; the runtime image never includes them.
FROM deps AS test
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 mvn -B -ntp test

FROM deps AS build
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 mvn -B -ntp -q package -DskipTests

FROM eclipse-temurin:21-jre-alpine AS runtime
WORKDIR /app
# Quarkus fast-jar layout: dependencies change least often, so copy them first.
COPY --from=build /src/target/quarkus-app/lib/ ./lib/
COPY --from=build /src/target/quarkus-app/*.jar ./
COPY --from=build /src/target/quarkus-app/app/ ./app/
COPY --from=build /src/target/quarkus-app/quarkus/ ./quarkus/
USER 10001
EXPOSE 8080
ENV JAVA_OPTS="-XX:MaxRAMPercentage=70 -XX:+ExitOnOutOfMemoryError"
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar quarkus-run.jar"]
